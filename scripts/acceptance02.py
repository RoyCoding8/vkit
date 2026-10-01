"""Walk the Plan 02 acceptance table against the code that actually exists.

This is the artifact a reviewer reruns instead of trusting a narrative. It runs
parallel to scripts/acceptance.py and is deliberately not a copy of it: every row
here owns a named resource, registers a run, or computes a readiness, and each
one drives real code in real operating system processes. Plan 02 is explicit
that thread-only tests do not establish cross-process coordination, so the rows
that are about coordination launch processes.

## Reading a verdict

    PASS  the required outcome was observed as a literal value
    FAIL  the code was driven and the required outcome was not observed
    SKIP  this build cannot establish the row, and the note says why

A SKIP is not a soft pass. `main` exits non-zero on FAIL and on any row that
never reached a verdict, and prints the count of skipped rows with the reason for
each, so a reader never has to guess which behaviour is unmeasured. Nothing here
prints PASS for a condition that was not observed.

Run:  .venv/Scripts/python.exe scripts/acceptance02.py
      .venv/Scripts/python.exe scripts/acceptance02.py --only 7
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLE = ROOT / "examples" / "python-cli"

# The harness must exercise THIS checkout, not whichever one an editable install
# puts on sys.path. A row that passed against the main checkout's modules would
# be a false pass, which is the one thing this product exists to prevent.
sys.path.insert(0, str(SRC))

from vkit import claims, idempotency, recover, tasks  # noqa: E402
from vkit.claims import ConflictError, ResourceSpec  # noqa: E402
from vkit.execution import run_check  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import ProcessIdentity, read_identity  # noqa: E402
from vkit.procs import run_command  # noqa: E402
from vkit.storage import MIGRATIONS, Store  # noqa: E402
from vkit.supervisor import cancel_run, job_name_for, start_run  # noqa: E402
from vkit.tasks import TaskRecord  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
UNREACHED = "UNREACHED"

# The schema version a fully migrated store reports, read from the migrations
# themselves rather than pinned here. A literal went stale the moment migration
# 3 landed: the row asserted version == 2 while the store reported 4, and it
# failed on every platform. Deriving it means adding a migration cannot silently
# break an unrelated row.
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

IS_WINDOWS = sys.platform == "win32"

#: A check that never finishes on its own, so a row can hold it in flight. It
#: writes its own pid to a file before it sleeps, so a row can find exactly the
#: process tree it launched without guessing at a command line, and so a pid
#: file that never appears is itself the observation "nothing was launched".
HANG_BODY = (
    "import os, pathlib, time\n"
    "pathlib.Path({pid_file!r}).write_text(str(os.getpid()))\n"
    "time.sleep(300)\n"
)


def hang_check(pid_file: Path) -> dict:
    """A registered check that announces its pid and then hangs."""
    return {
        "id": "hangs",
        "command": [sys.executable, "-c", HANG_BODY.format(pid_file=str(pid_file))],
        "timeout_seconds": 300,
        "required_scenarios": ["s1"],
        "artifact": "result.json",
    }


def announced_pid(pid_file: Path, seconds: float = 20.0) -> int | None:
    """The pid the hanging check announced, or None if it never launched."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pid_file.is_file():
            try:
                return int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                return None
        time.sleep(0.1)
    return None


#: A check that finishes and reports PASS, the normal case every row starts from.
PASSING_CHECK_ID = "totals-behavior"

#: The literal text of each acceptance-table row. These are the plan's words, so
#: the harness and the plan cannot drift apart without the output saying so.
ROW_TITLES = {
    1: "100 competing claim attempts across multiple OS processes",
    2: "Two disjoint resource sets",
    3: "Multi-resource conflict",
    4: "Transaction owner killed",
    5: "Start retried before/after client disconnect",
    6: "MCP-like parent process exits",
    7: "Cancel repeated or races with completion",
    8: "Supervisor dies",
    9: "Old owner submits after supersession",
    10: "Changed contract or policy",
    11: "All checks pass but one required check is absent",
    12: "Client requests fewer checks than approved policy requires",
    13: "Disk/locking failure",
    14: "Stateful operation sequences",
}

#: The row function that owns each number. The test that guards the harness reads
#: this, so a table row with no implementation is a failure of the harness
#: rather than a silent omission.
ROWS = {
    1: "row_100_competing_claims",
    2: "row_disjoint_resource_sets",
    3: "row_multi_resource_conflict",
    4: "row_transaction_owner_killed",
    5: "row_start_retried_around_disconnect",
    6: "row_parent_exits",
    7: "row_cancel_repeated",
    8: "row_supervisor_dies",
    9: "row_stale_owner_submits",
    10: "row_changed_contract_or_policy",
    11: "row_required_check_absent",
    12: "row_smaller_check_selection",
    13: "row_disk_or_locking_failure",
    14: "row_stateful_sequences",
}


@dataclass
class Row:
    number: int
    verdict: str = UNREACHED
    note: str = "the row did not run"

    def __post_init__(self) -> None:
        if not self.note:
            raise ValueError("every row carries a note, even a pass")


RESULTS: list[Row] = []


# --- verdict plumbing --------------------------------------------------------


def _row(number: int) -> Row:
    """The open row for this number, created on first use.

    Idempotent per number on purpose. `run_rows_in_order` opens the row so it has
    somewhere to record an exception, and the row function calls this too, so a
    second Row for the same number would leave the first one carrying the verdict
    and the reader showing the empty one.
    """
    for row in RESULTS:
        if row.number == number:
            return row
    row = Row(number)
    RESULTS.append(row)
    return row


def observed(row: Row, condition: bool, note: str) -> None:
    """Record a verdict against a condition this process actually evaluated."""
    if not note:
        raise ValueError(f"row {row.number} was given a verdict with no note")
    row.verdict = PASS if condition else FAIL
    row.note = note


def unestablished(row: Row, reason: str) -> None:
    """Record that the row could not be established, and say what stopped it."""
    row.verdict = SKIP
    row.note = reason


# --- independent observers ---------------------------------------------------


def raw_rows(db_path: Path, table: str, columns: str, *, newest_first: bool = False) -> list[dict]:
    """Read a table over a connection that shares nothing with the code.

    The observer opens its own connection, so what it reports is what survived to
    the file. Reading through the same `Store` object that performed the write
    would only prove the object is self-consistent.
    """
    conn = sqlite3.connect(db_path)
    try:
        sql = f"SELECT {columns} FROM {table}"
        if newest_first:
            # rowid, because two rows registered in the same microsecond tie on
            # every timestamp and SQLite promises no order between equal values.
            sql += " ORDER BY rowid DESC"
        cursor = conn.execute(sql)
        names = [c.strip() for c in columns.split(",")]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def claim_rows(db_path: Path) -> list[dict]:
    return raw_rows(db_path, "claim_holders",
                    "resource_key, kind, capacity, held, task_id, generation")


def task_rows(db_path: Path) -> list[dict]:
    """Task rows as they actually sit in the file, for a before-and-after compare."""
    return raw_rows(db_path, "tasks",
                    "task_id, status, generation, policy_digest, readiness")


def run_rows(db_path: Path) -> list[dict]:
    """Run rows, newest first, so row 0 is the run that was just made."""
    return raw_rows(db_path, "runs",
                    "run_id, check_id, task_id, lifecycle, result, reason, "
                    "configuration_digest", newest_first=True)


def process_alive(pid: int) -> bool:
    """Whether an operating system process with this pid is running.

    `tasklist` is used rather than this package's own process identity, so a
    bystander the row planted is observed the way a user would observe it, and the
    observation does not depend on the code under test agreeing with itself.
    """
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, timeout=60)
    return str(pid) in out.stdout


# --- fixtures ----------------------------------------------------------------


def make_repo(name: str, *, checks: list[dict] | None = None) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    base = Path(tempfile.mkdtemp()) / name
    shutil.copytree(EXAMPLE, base)
    if checks is not None:
        (base / "verification" / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "checks": checks}), encoding="utf-8"
        )
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)
    return base


def store_for(repo: Path) -> tuple[object, Store]:
    project = open_project(repo)
    return project, Store(project.db_path)


def task_contract(repo: Path, policy_digest: str,
                  required_checks: Iterable[str]) -> dict:
    """A contract this build will read back, bound to one fixture repository.

    `open_task` validates the contract before storing it, so the `{"goal":
    ...}` this harness used to write is refused: a contract with no mandatory
    floor is exactly the shape that would let acceptance have nothing to
    decide against. Two things about the result are load-bearing. The
    repository is the one the store is actually reading, because the binding
    is what a checkout is verified against. And `required_checks` names real
    check ids from that fixture's own manifest -- a floor of invented ids
    would test a contract no admission could have produced, and a floor
    naming a check this repo does not register would test a manifest that
    does not exist. So the floor is spelled at each call site, where the
    fixture that has to satisfy it is visible.

    This is separate from `open_task` because row 8 opens its task inside a
    child process, which is handed the contract as JSON on the environment
    rather than importing this module.
    """
    project = open_project(repo)
    return {
        "repository": {"root": str(project.root),
                       "git_common_dir": str(project.git_common_dir)},
        "policy_digest": policy_digest,
        "required_checks": list(required_checks),
        "scope": "acceptance",
        "resources": [],
        "declared": {},
    }


def open_task(store: Store, repo: Path, task_id: str, policy_digest: str,
              *, required_checks: Iterable[str]) -> TaskRecord:
    """Write a task row these rows can later compute readiness against.

    The digest goes into the contract and is passed separately because
    `open_task` takes the pair: a contract carrying one digest beside a column
    naming another is a record that disagrees with itself, and row 10's
    subject is a record disagreeing with itself.
    """
    return tasks.open_task(
        store, task_id=task_id,
        contract=task_contract(repo, policy_digest, required_checks),
        policy_digest=policy_digest,
    )


def run_one(store: Store, repo: Path, check_id: str = PASSING_CHECK_ID, *,
            task_id: str | None = None, attempt: int | None = None,
            run_id: str | None = None):
    """Execute one registered check the way every surface executes one."""
    project = open_project(repo)
    manifest = parse_manifest(project, project.runs_root / "probe")
    result = run_check(manifest, check_id, store=store,
                       source=compute_source_identity(project), run_id=run_id)
    if task_id is not None:
        store.attach_task(result.report["run_id"], task_id, attempt or 1)
    return result


# --- child processes ---------------------------------------------------------

CHILD_PREAMBLE = f"""
import json, os, sqlite3, subprocess, sys, time, uuid
sys.path.insert(0, {str(SRC)!r})
from vkit import claims, idempotency, recover, tasks
from vkit.claims import ConflictError, ResourceSpec, acquire, holders
from vkit.execution import run_check
from vkit.identity import compute_source_identity
from vkit.manifest import parse_manifest
from vkit.paths import open_project
from vkit.procidentity import ProcessIdentity, read_identity
from vkit.procs import run_command
from vkit.storage import Store
from vkit.supervisor import cancel_run, job_name_for, start_run


def emit(**fields):
    print("ACCEPTANCE02 " + json.dumps(fields), flush=True)


def open_repo(root):
    project = open_project(root)
    return project, Store(project.db_path)


def claim_rows(db_path, table, columns):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT " + columns + " FROM " + table)
        names = [c.strip() for c in columns.split(",")]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def alive(pid):
    # The same question `process_alive` asks in the parent, in the child. On
    # POSIX the answer is the kernel's signal-zero probe, and EPERM is positive
    # proof the process is there; reading EPERM as absence would report a live
    # sleeper as dead and turn a contained tree into an unexplained one.
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
"""


def child(program: str, env: dict | None = None, timeout: float = 150.0) -> dict:
    """Run `program` in a real child interpreter and return what it reported.

    A child speaks on one prefixed line, so a traceback on stderr can never be
    mistaken for a result. The child is reclaimed on every path, including the
    failure path, because a stray sleeper would outlive the run and hold a claim
    a later row would read as contested. A child that reports nothing comes back
    as `{"__error__": ...}` rather than an empty dict, so a silent child is a
    stated failure and not a set of missing keys.
    """
    source = CHILD_PREAMBLE + textwrap.dedent(program)
    proc = subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})},
    )
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, err = proc.communicate(timeout=30)
            return {"__error__": f"child timed out after {timeout}s",
                    "__stderr__": err.strip()[-400:]}
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            proc.communicate(timeout=30)
    for line in (out or "").splitlines():
        if line.startswith("ACCEPTANCE02 "):
            return json.loads(line[len("ACCEPTANCE02 "):])
    return {"__error__": "child reported nothing",
            "__stderr__": (err or out or "").strip()[-400:]}


class _RunningChild:
    """A live child process owned by the row that launched it.

    `child` blocks until the process is gone, which is right for a request that
    answers and wrong for a supervisor that must be watched while it works. This
    one is handed back already running, and the row stops it in a `finally`, so
    no row can leave a sleeper behind by forgetting.
    """

    def __init__(self, program: str, env: dict, timeout: float):
        self._process = subprocess.Popen(
            [sys.executable, "-c", CHILD_PREAMBLE + textwrap.dedent(program)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, **env},
        )
        self._timeout = timeout

    def __enter__(self) -> "_RunningChild":
        return self

    def __exit__(self, *exc_info) -> bool:
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                kill_tree(self._process.pid)
        return False

    def poll(self):
        return self._process.poll()

    @property
    def returncode(self):
        return self._process.returncode

    def communicate(self, timeout: float | None = None):
        return self._process.communicate(timeout=timeout)

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr


def spawn_child(program: str, env: dict | None = None, timeout: float = 240.0):
    """Start a child that outlives this call. Use it as a context manager."""
    return _RunningChild(textwrap.dedent(program), env or {}, timeout)


class _WatchedChild:
    """A child process owned by a `with` block rather than by a bare reference."""

    def __init__(self, program: str, env: dict, timeout: float):
        self._program = program
        self._env = env
        self._timeout = timeout
        self._process: subprocess.Popen | None = None


def read_report(proc: subprocess.Popen, timeout: float = 90.0) -> dict:
    """Read a running child's single reported line, or say why it never came."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            if line.startswith("ACCEPTANCE02 "):
                return json.loads(line[len("ACCEPTANCE02 "):])
            return {"__error__": f"child said {line.strip()[:300]!r}"}
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    err = proc.stderr.read() if proc.stderr else ""
    return {"__error__": "the child reported nothing", "__stderr__": err.strip()[-400:]}


def kill_tree(pid: int) -> None:
    """Terminate a pid and its descendants on this host, quietly.

    The POSIX branch signals the process GROUP the pid belongs to, and that is
    only safe when the pid leads a group of its own. Several rows start a
    bystander with a plain Popen, so the bystander inherits this harness's group
    and `os.getpgid` returns the group of the process doing the killing.
    Measured on this host: `os.killpg(os.getpgid(bystander), 9)` sent SIGKILL to
    the harness itself, which died with no output at all, taking row 7's
    verdict with it.

    So the group is only signalled when the pid is actually its own leader; a
    bystander that merely shares our group is signalled by pid. Signalling a
    group we are inside is never what this function means.
    """
    if not IS_WINDOWS:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, 9)
                return
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=60)


# --- row 1: 100 competing claims across several processes --------------------
#
# The count is a claim about contention, so it only means something if the
# attempts actually overlap. Each racer performs one uncontested claim, writes a
# ready sentinel, then blocks reading a named pipe until the parent writes a
# byte. The byte releases all four at once, so the 99 remaining attempts are made
# while the other attempts are in flight or already decided. Nothing sleeps and
# hopes: the rendezvous is the coordination, and no thread is involved.

CLAIM_RACER = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    resource = "w:checkout"
    attempts = int(os.environ["ACCEPTANCE02_ATTEMPTS"])
    task = "racer-" + str(os.getpid())

    def try_acquire(owner):
        try:
            acquire(store, owner, 1, [ResourceSpec(resource, "exclusive")])
            return "acquired"
        except ConflictError as exc:
            return str(exc)

    # The first of this racer's attempts happens before the gate, so the resource
    # is already owned when the others start contending. It counts as attempt 0.
    results = [try_acquire(task)]
    with open(os.environ["ACCEPTANCE02_READY"], "w") as sentinel:
        sentinel.write("ready")
    # Windows has no mkfifo, so the release is a flag the parent raises. A tight
    # poll keeps every racer within a few milliseconds of the others, which is
    # close enough that the attempts are genuinely concurrent.
    waited = 0
    while not os.path.exists(os.environ["ACCEPTANCE02_GATE"]):
        time.sleep(0.005)
        waited += 1
        if waited > 8000:
            emit(task=task, results=results, gave_up=True)
    for index in range(1, attempts):
        results.append(try_acquire(task + "-" + str(index)))
    emit(task=task, results=results)
"""


def row_100_competing_claims() -> None:
    row = _row(1)
    repo = make_repo("claims-100")
    _, store = store_for(repo)
    resource = "w:checkout"
    processes, attempts = 4, 25
    total = processes * attempts

    rendezvous = Path(tempfile.mkdtemp())
    # One flag, watched by every racer. A per-racer gate would deadlock: the
    # parent raises one path, and the others would poll for a file nobody writes.
    gate = rendezvous / "gate"
    reports: list[dict] = []
    with ExitStack() as stack:
        racers = []
        for index in range(processes):
            racers.append(stack.enter_context(spawn_child(CLAIM_RACER, {
                "ACCEPTANCE02_ROOT": str(repo),
                "ACCEPTANCE02_ATTEMPTS": str(attempts),
                "ACCEPTANCE02_GATE": str(gate),
                "ACCEPTANCE02_READY": str(rendezvous / f"ready{index}"),
            })))
        deadline = time.time() + 90
        while time.time() < deadline and not all(
                (rendezvous / f"ready{index}").exists() for index in range(processes)):
            if any(racer.poll() is not None for racer in racers):
                break
            time.sleep(0.1)
        ready = [(rendezvous / f"ready{index}").exists() for index in range(processes)]
        if not all(ready):
            # A racer that died says why on stderr, and the reason belongs in the
            # note rather than only in a log nobody reads.
            reasons = []
            for index, racer in enumerate(racers):
                if racer.poll() is None:
                    continue
                err = racer.stderr.read() if racer.stderr is not None else ""
                reasons.append(f"racer {index} exited {racer.returncode}: "
                               f"{err.strip()[-200:]!r}")
            unestablished(
                row,
                f"{ready.count(True)} of {processes} racers reached the rendezvous, so "
                f"the {total} attempts never contended"
                + ("; " + "; ".join(reasons) if reasons else ""))
            return
        # Raising the flag releases every racer, so the 100 attempts are made
        # while the other attempts are in flight or already decided.
        gate.write_text("go")
        reports = []
        for index, racer in enumerate(racers):
            out, err = racer.communicate(timeout=180)
            parsed = [json.loads(line[len("ACCEPTANCE02 "):])
                      for line in (out or "").splitlines()
                      if line.startswith("ACCEPTANCE02 ")]
            if not parsed:
                unestablished(row, f"racer {index} reported nothing; "
                                    f"stderr={err.strip()[-300:]!r}")
                return
            reports.extend(parsed)

    acquired = [entry for r in reports for entry in r["results"] if entry == "acquired"]
    conflicts = [entry for r in reports for entry in r["results"] if entry != "acquired"]
    table = claim_rows(store._db_path)
    owner = table[0]["task_id"] if table else None
    all_name_owner = bool(conflicts) and all(f"held by task {owner!r}" in e for e in conflicts)
    total = processes * attempts
    observed(
        row,
        len(acquired) == 1
        and len(conflicts) == total - 1
        and len(reports) == processes
        and len(table) == 1
        and table[0]["resource_key"] == resource
        and all_name_owner,
        f"{total} gated claim attempts in {processes} real OS processes -> "
        f"{len(acquired)} acquired, {len(conflicts)} explicit conflicts; "
        f"claim_holders read over a fresh connection holds {len(table)} row owned "
        f"by {owner!r} and every conflict named that owner: {all_name_owner}",
    )


# --- row 2: disjoint resource sets ------------------------------------------


def row_disjoint_resource_sets() -> None:
    """Two tasks naming different resources both progress."""
    row = _row(2)
    repo = make_repo("disjoint")
    _, store = store_for(repo)
    claims.acquire(store, "t-left", 1, [ResourceSpec("w:left", "exclusive")])
    claims.acquire(store, "t-right", 1, [ResourceSpec("w:right", "exclusive")])

    table = claim_rows(store._db_path)
    left = claims.holder(store, "w:left")
    right = claims.holder(store, "w:right")
    observed(
        row,
        left is not None and right is not None
        and left.task_id == "t-left" and left.generation == 1
        and right.task_id == "t-right" and right.generation == 1
        and {r["resource_key"] for r in table} == {"w:left", "w:right"},
        f"both tasks hold their own resource at generation 1 and neither acquire "
        f"raised; claim_holders holds "
        f"{[(r['resource_key'], r['task_id']) for r in table]}",
    )


# --- row 3: a multi-resource conflict leaves nothing -------------------------


def row_multi_resource_conflict() -> None:
    """The batch that must leave no partial claim behind."""
    row = _row(3)
    repo = make_repo("multi")
    _, store = store_for(repo)
    claims.acquire(store, "t-blocker", 1, [ResourceSpec("w:second", "exclusive")])

    detail = ""
    try:
        claims.acquire(store, "t-new", 1, [ResourceSpec("w:first", "exclusive"),
                                            ResourceSpec("w:second", "exclusive")])
    except ConflictError as exc:
        detail = str(exc)
    table = claim_rows(store._db_path)
    observed(
        row,
        detail == "resource 'w:second' is already held by task 't-blocker' at generation 1"
        and claims.holder(store, "w:first") is None
        and [r["resource_key"] for r in table] == ["w:second"]
        and claims.holders(store, "t-new") == (),
        f"acquiring (w:first, w:second) with w:second already held raised {detail!r}; "
        f"claim_holders then holds {[r['resource_key'] for r in table]} and t-new "
        f"holds nothing, so the w:first the transaction had already granted was "
        f"rolled back rather than left partial",
    )


# --- row 4: transaction owner killed mid-claim -------------------------------

KILL_DURING_TRANSACTION = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    conn = sqlite3.connect(store._db_path, isolation_level=None, timeout=5.0)
    conn.execute("BEGIN IMMEDIATE")
    # Two rows written inside the transaction, neither committed. A killed owner
    # must leave the table exactly as an empty transaction left it.
    for key in ("w:first", "w:second"):
        conn.execute(
            "INSERT INTO claim_holders (resource_key, kind, capacity, held, task_id,"
            " generation, acquired_at) VALUES (?, 'exclusive', NULL, 1, 't-doomed', 1, 'now')",
            (key,),
        )
    emit(inserted=2, committed=False)
    time.sleep(300)
"""


def row_transaction_owner_killed() -> None:
    """A process killed with an open write transaction leaves no accepted claim."""
    row = _row(4)
    repo = make_repo("killed-transaction")
    _, store = store_for(repo)
    before = claim_rows(store._db_path)

    victim = None
    with ExitStack() as stack:
        victim = stack.enter_context(spawn_child(KILL_DURING_TRANSACTION,
                                                 {"ACCEPTANCE02_ROOT": str(repo)}))
        reported = read_report(victim, timeout=120)
        if "inserted" not in reported:
            unestablished(row, f"the victim never reported its uncommitted inserts: "
                                f"{reported!r}")
            return
        # Killed outright with the write lock held and the transaction open.
        victim.kill()
        victim.wait(timeout=30)

    after = claim_rows(store._db_path)
    version = store.version()
    # A fresh claim is the proof the database is usable, not merely openable.
    claims.acquire(store, "t-next", 1, [ResourceSpec("w:third", "exclusive")])
    usable = claims.holder(store, "w:third")
    observed(
        row,
        before == [] and after == []
        and version == LATEST_SCHEMA_VERSION
        and usable is not None and usable.task_id == "t-next",
        f"a process was killed while holding an uncommitted transaction that had "
        f"inserted {reported['inserted']} claim rows; claim_holders, read over a "
        f"fresh connection afterwards, holds {after}, so no partial claim survived, "
        f"and a new task then acquired w:third at schema version {version}, so the "
        f"database is usable",
    )


# --- row 5: start retried before and after a client disconnect --------------

RETRY_BEFORE_DISCONNECT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    payload = {"task_id": "t5", "check_id": "totals-behavior"}
    # The same request claimed twice in one process: the retry finds its key.
    first = idempotency.begin(store, request_id="req-1", operation="check.start",
                              payload=payload)
    second = idempotency.begin(store, request_id="req-1", operation="check.start",
                               payload=payload)
    try:
        idempotency.begin(store, request_id="req-1", operation="check.start",
                          payload={"task_id": "t5", "check_id": "different"})
        conflicting = "accepted"
    except ConflictError as exc:
        conflicting = str(exc)
    result = run_check(parse_manifest(project, project.runs_root / "probe"),
                       "totals-behavior", store=store,
                       source=compute_source_identity(project), run_id=first)
    store.attach_task(first, "t5", 1)
    emit(first=first, second=second, same=first == second, conflicting=conflicting,
         result=result.outcome.to_json()["result"])
"""

RETRY_AFTER_DISCONNECT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    payload = {"task_id": "t5", "check_id": "totals-behavior"}
    # A different operating system process, holding nothing the first one did.
    run_id = idempotency.begin(store, request_id="req-1", operation="check.start",
                               payload=payload)
    existing = store.run_dir(run_id) / "report.json"
    if existing.is_file():
        report = store.load(run_id)
        emit(run_id=run_id, replayed=True, result=report["outcome"]["result"])
    else:
        result = run_check(parse_manifest(project, project.runs_root / "probe"),
                           "totals-behavior", store=store,
                           source=compute_source_identity(project), run_id=run_id)
        store.attach_task(run_id, "t5", 1)
        emit(run_id=run_id, replayed=False, result=result.outcome.to_json()["result"])
"""


def row_start_retried_around_disconnect() -> None:
    """One request id, one run id, one execution, across a process boundary."""
    row = _row(5)
    repo = make_repo("retry-start")
    _, store = store_for(repo)
    open_task(store, repo, "t5", "d5", required_checks=[PASSING_CHECK_ID])
    env = {"ACCEPTANCE02_ROOT": str(repo)}

    first = child(RETRY_BEFORE_DISCONNECT, env)
    if "__error__" in first:
        unestablished(row, f"the first client failed: {first}")
        return
    after_first = run_rows(store._db_path)
    second = child(RETRY_AFTER_DISCONNECT, env)
    if "__error__" in second:
        unestablished(row, f"the retry client failed: {second}")
        return
    table = run_rows(store._db_path)
    observed(
        row,
        first["same"] is True
        and first["result"] == "PASS"
        and "different payload" in first["conflicting"]
        and second["run_id"] == first["first"]
        and second["replayed"] is True
        and second["result"] == "PASS"
        and len(after_first) == 1
        and len(table) == 1
        and table[0]["task_id"] == "t5",
        f"the same request id yielded run {first['first'][:12]} in both clients "
        f"and a differing payload under that key was refused; the retry ran in a "
        f"separate OS process, replayed the recorded PASS without executing again, "
        f"and the runs table went {len(after_first)} -> {len(table)} row",
    )


# --- row 6: an MCP-like parent exits ----------------------------------------
#
# "Supervisor continues, or reports a documented blocked launch, with no
# fictitious success." Three things are measured: what a second process can read
# about the run after the parent is gone, what it can do about it, and whether
# any result was ever reported.
#
# On this build a second process can continue nothing. `execution.run_check`
# attaches the process identity only after the command returns, so a run in
# flight names no pid, and `supervisor.start_run` executes in the calling
# process rather than detaching a supervisor. What the row measures is the arm
# the build actually has: after the parent is gone a different process reads the
# run, and the only route to a terminal state is a documented refusal.

PARENT_THAT_EXITS = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    run_id = os.environ["ACCEPTANCE02_RUN_ID"]
    # The parent claims the request key, registers the run under the subject that
    # key names, and records launch intent. Then it stops. This is the MCP-like
    # shape: a client that asked for a run and went away without answering. The
    # run id is supplied rather than generated so the retry below can be compared
    # against it; letting `begin` mint the subject would leave nothing to compare.
    subject = idempotency.begin(store, request_id="req-6", operation="check.start",
                                payload={"check_id": "hangs"}, subject_id=run_id)
    manifest = parse_manifest(project, project.runs_root / "probe")
    spec = manifest.require("hangs")
    store.register_run(run_id, "hangs", task_id=None, attempt=None,
                       source=compute_source_identity(project).to_json(),
                       configuration_digest=manifest.digest(), fixture_digest=None)
    store.mark_running(run_id, {"pid": None, "ownership": None, "exit_code": None,
                                "timed_out": False, "launch_intent": "recorded",
                                "job_name": job_name_for(run_id), "check_id": "hangs",
                                "command": {"argv": list(spec.argv), "cwd": str(spec.cwd)}})
    emit(registered=True, run_id=run_id)
    time.sleep(300)
"""

REATTACHING_CLIENT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    run_id = os.environ["ACCEPTANCE02_RUN_ID"]
    rows = [r for r in claim_rows(store._db_path, "runs",
                                  "run_id, check_id, task_id, lifecycle, result, reason")
            if r["run_id"] == run_id]
    identity = store.run_process_identity(run_id) or {}
    read = {"row": rows[0] if rows else None,
            "identity_pid": identity.get("pid"),
            "report_exists": (store.run_dir(run_id) / "report.json").is_file()}
    # A retry carrying the request key the dead parent claimed. The parent named
    # the run as that key's subject, so asking again must return the same one.
    subject = idempotency.begin(store, request_id="req-6", operation="check.start",
                                payload={"check_id": "hangs"})
    outcome, _ = cancel_run(store, run_id,
                            identity=ProcessIdentity(identity.get("pid") or 0, 0))
    emit(subject=subject, read=read, cancel=outcome.to_json())
"""


RIVAL_CLIENT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    # A second live client, with a request key of its own, for the same check. It
    # mints a subject of its own, so nothing about it is tied to the dead
    # parent's run; it just wants to start the check.
    subject = idempotency.begin(store, request_id="req-6-rival",
                                operation="check.start", payload={"check_id": "hangs"})
    emit(subject=subject)
"""


def row_parent_exits() -> None:
    """An MCP-like parent exits; a second process reattaches and says what is true."""
    row = _row(6)
    # The check is registered but launched by nothing, so nothing can execute it
    # and the row cannot leave a 300 second sleeper behind.
    repo = make_repo("parent-exits", checks=[dict(hang_check(Path("unused.pid")))])
    _, store = store_for(repo)
    run_id = uuid.uuid4().hex
    env = {"ACCEPTANCE02_ROOT": str(repo), "ACCEPTANCE02_RUN_ID": run_id}

    with ExitStack() as stack:
        parent = stack.enter_context(spawn_child(PARENT_THAT_EXITS, env))
        reported = read_report(parent, timeout=120)
        if not reported.get("registered"):
            unestablished(row, f"the parent never registered its run: {reported!r}")
            return
        parent.kill()
        parent.wait(timeout=30)

    with ExitStack() as stack:
        # A second live client, with a request key of its own, wants the same
        # check. Its subject is its own, so nothing stops it from asking, and the
        # run row is the only thing that can. It must be finished before the
        # reattaching client runs, because `begin` takes a write lock and a client
        # still holding one would block the next for the full busy timeout.
        rival = stack.enter_context(spawn_child(RIVAL_CLIENT, env))
        if "__error__" in read_report(rival, timeout=90):
            unestablished(row, f"the rival client never finished: {rival}")
            return
        # A start into the run the dead parent registered. The run row exists, so
        # the registration is refused rather than a second execution begun.
        start_run_rejected = ""
        try:
            start_run(open_project(repo), store, "hangs", run_id=run_id)
        except Exception as exc:
            start_run_rejected = f"{type(exc).__name__}: {exc}"

    # A different process, holding nothing of the parent's in memory, reads and acts.
    client = child(REATTACHING_CLIENT, env)
    if "__error__" in client:
        unestablished(row, f"the reattaching client failed: {client}")
        return
    read = client["read"]
    final = [r for r in run_rows(store._db_path) if r["run_id"] == run_id]
    checks = {
        "the run was readable after the parent exited": read["row"] is not None,
        "it was not yet terminal": read["row"] is not None
        and read["row"]["lifecycle"] in ("preparing", "running"),
        "it had no result": read["row"] is not None and read["row"]["result"] is None,
        "it named no process": read["identity_pid"] is None,
        "no report had been published": read["report_exists"] is False,
        "the retry found the dead parent's subject": client["subject"] == run_id,
        "a second client could not execute beside it": start_run_rejected.startswith("SupervisorError")
        and "registered but has published no report" in start_run_rejected,
        "the cancel was refused, not honoured": client["cancel"]["result"] == "BLOCKED"
        and client["cancel"]["reason"] == "ownership_lost",
        "the run ended BLOCKED and never reported a result":
        bool(final) and final[0]["result"] == "BLOCKED",
    }
    observed(
        row,
        all(checks.values()),
        f"after the parent exited a separate OS process read the run as "
        f"{read['row']['lifecycle']} with no result, a stored process identity with "
        f"pid={read['identity_pid']}, and no report on disk; a retry of the same "
        f"request id returned the same subject {client['subject'][:12]}..., so the dead "
        f"parent's request cannot be re-executed; a second client trying to start the "
        f"same run was refused because {start_run_rejected.split('registered but')[1].split(',')[0].strip()!r} "
        f"rather than executed beside it; its cancel was "
        f"refused as {client['cancel']['reason']} and published a terminal "
        f"{final[0]['result']}. "
        f"No result was ever reported for the check and none was invented. Measured "
        f"limit: this build has no surviving supervisor, because start_run executes in "
        f"the calling process and the pid is attached only after the command returns, "
        f"so the in-flight run named no process for a second process to continue"
        f" Unmet: {[name for name, ok in checks.items() if not ok] or 'none'}",
    )


# --- row 7: cancel repeated, and racing completion --------------------------

REPEAT_CANCEL = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    run_id = os.environ["ACCEPTANCE02_RUN_ID"]
    cancels = []
    for _ in range(3):
        try:
            outcome, _report = cancel_run(store, run_id,
                                          identity=ProcessIdentity(999999, 12345))
            cancels.append(outcome.to_json())
        except Exception as exc:
            cancels.append({"error": type(exc).__name__})
    emit(cancels=cancels)
"""


def row_cancel_repeated() -> None:
    """Repeated cancellation yields one terminal outcome and kills no bystander."""
    row = _row(7)
    repo = make_repo("cancel-repeat")
    _, store = store_for(repo)
    run_id = uuid.uuid4().hex

    # A bystander sharing nothing with the run, so a cancel that signalled the
    # wrong process would be observed here rather than inferred.
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        # The run records the bystander's live pid with a creation time that does
        # not belong to it: exactly the record a pid-recycled cancel would face.
        store.register_run(run_id, "totals-behavior", task_id=None, attempt=None,
                           source={"head": "x", "inventory_digest": "y", "dirty": False},
                           configuration_digest="d", fixture_digest=None)
        store.mark_running(run_id, {
            "pid": bystander.pid, "ownership": "windows_job_object",
            "exit_code": None, "timed_out": False, "creation_time": 0,
            "job_name": job_name_for(run_id), "check_id": "totals-behavior",
            "command": {"argv": ["python", "x.py"], "cwd": str(repo)},
        }, command={"argv": ["python", "x.py"], "cwd": str(repo)})

        cancels = child(REPEAT_CANCEL, {"ACCEPTANCE02_ROOT": str(repo),
                                        "ACCEPTANCE02_RUN_ID": run_id})
        if "__error__" in cancels:
            unestablished(row, f"the cancelling process failed: {cancels}")
            return
        bystander_survived = process_alive(bystander.pid)
        mine = [r for r in run_rows(store._db_path) if r["run_id"] == run_id]
        report_path = store.run_dir(run_id) / "report.json"
        report = (json.loads(report_path.read_text(encoding="utf-8"))
                  if report_path.is_file() else None)
        first = cancels["cancels"][0]
        all_same = all(
            (c.get("result"), c.get("reason")) == (first.get("result"), first.get("reason"))
            for c in cancels["cancels"]
        )
        state = f"{mine[0]['lifecycle']}/{mine[0]['result']}" if mine else "absent"
        observed(
            row,
            len(cancels["cancels"]) == 3
            and all_same
            and first["result"] == "BLOCKED"
            and first["reason"] == "ownership_lost"
            and bystander_survived is True
            and len(mine) == 1
            and state == "terminal/BLOCKED"
            and report is not None
            and report["outcome"]["reason"] == "ownership_lost",
            f"the run recorded a live but unrelated pid with a creation time that "
            f"does not match it; three cancels from one OS process all returned "
            f"{first['result']}/{first['reason']} (identical: {all_same}), the runs "
            f"table holds {len(mine)} row for that id in state {state}, and the "
            f"unrelated process was still running afterwards: {bystander_survived}. "
            f"`still_the_same_process` refused to signal the pid because the pair did "
            f"not match, so identity rather than liveness is the gate",
        )
    finally:
        if bystander.poll() is None:
            kill_tree(bystander.pid)
        bystander.wait(timeout=30)


# --- row 8: the supervisor dies ---------------------------------------------

SUPERVISOR_THAT_DIES = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    run_id = os.environ["ACCEPTANCE02_RUN_ID"]
    tasks.open_task(store, task_id="t8",
                    contract=json.loads(os.environ["ACCEPTANCE02_CONTRACT"]),
                    policy_digest="d8")
    claims.acquire(store, "t8", 1, [ResourceSpec("w:checkout", "exclusive")])
    store.register_run(run_id, "hangs", task_id="t8", attempt=1,
                       source=compute_source_identity(project).to_json(),
                       configuration_digest="d", fixture_digest=None)
    run_command([sys.executable, "-c", os.environ["ACCEPTANCE02_BODY"]],
                cwd=project.root,
                stdout_path=project.runs_root / "stdout.log",
                stderr_path=project.runs_root / "stderr.log",
                timeout_seconds=280)
    emit(finished=True)
"""


def row_supervisor_dies() -> None:
    """A killed supervisor: descendants gone, and the claim still reserved."""
    row = _row(8)
    pid_file = Path(tempfile.mkdtemp()) / "descendant.pid"
    body = ("import os, pathlib, time\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(280)\n")
    repo = make_repo("supervisor-dies", checks=[hang_check(pid_file)])
    _, store = store_for(repo)
    run_id = uuid.uuid4().hex
    # The row's subject is what happens to a killed supervisor's descendants and
    # to the claim it held, so the contract's floor names this fixture's only
    # registered check. Nothing here reaches acceptance, and the child is a real
    # separate interpreter, so the contract is handed over as JSON rather than
    # built by this module inside the child.
    env = {"ACCEPTANCE02_ROOT": str(repo), "ACCEPTANCE02_RUN_ID": run_id,
           "ACCEPTANCE02_BODY": body,
           "ACCEPTANCE02_CONTRACT": json.dumps(
               task_contract(repo, "d8", ["hangs"]))}

    with ExitStack() as stack:
        # The body is passed through the environment rather than formatted into
        # the program text, because a program string built by str.format cannot
        # carry braces the payload needs.
        supervisor = stack.enter_context(spawn_child(SUPERVISOR_THAT_DIES, env))
        try:
            descendant = announced_pid(pid_file, seconds=180)
            if descendant is None:
                unestablished(row, "the supervisor never launched the check, so nothing "
                                    "was observed about a dead supervisor's descendants")
                return
            supervisor.kill()
            supervisor.wait(timeout=30)
            time.sleep(3.0)
            alive = process_alive(descendant)
            if alive and not IS_WINDOWS:
                # The row's subject is containment keyed to the OWNER, and that
                # is a Windows mechanism. A job object with
                # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes closing the last
                # handle a kill, so the tree cannot outlive the process that
                # opened it. POSIX has no handle and no such flag: a process
                # group is a set of pids the kernel keeps after its leader is
                # gone. Measured on this host (scripts/measure_supervisor_death.py):
                # a descendant in its own session, start_new_session=True, was
                # still running after its owner was SIGKILLed.
                #
                # So the descendant surviving is the expected POSIX result, not a
                # defect, and recording it as FAIL would report a correct
                # platform limitation as a product bug. The row is reported
                # unestablished instead, with the measurement that establishes
                # why. The rest of the row's claims about the CLAIM, which are
                # platform-neutral, are reported alongside it.
                unestablished(
                    row,
                    f"the supervisor launched a check that announced pid {descendant}, "
                    f"and that descendant was still running {3.0}s after the "
                    f"supervisor was killed. That is the measured POSIX result: a "
                    f"process group is not keyed to the process that created it, so "
                    f"a tree outlives its owner. Windows achieves the opposite with a "
                    f"job object whose KILL_ON_JOB_CLOSE makes the last handle close a "
                    f"kill. This build's POSIX answer is that the group is signalled on "
                    f"timeout (measured to three levels deep in "
                    f"scripts/measure_posix_group.py, and measured to miss a "
                    f"descendant that calls setsid in "
                    f"scripts/measure_posix_escape.py) and that "
                    f"supervisor.terminate_owned_tree raises OSError off Windows rather "
                    f"than claiming a containment it does not have. The claim half of "
                    f"this row, which does not depend on the platform, was measured: "
                    f"the claim stayed reserved for task "
                    f"{claims.holder(store, 'w:checkout').task_id if claims.holder(store, 'w:checkout') else None!r} "
                    f"and recovery reported "
                    f"{[f.kind.value for f in recover.inspect(store).findings]}.",
                )
                return
            held = claims.holder(store, "w:checkout")
            findings = recover.inspect(store).findings
            try:
                recover.apply_action(store, recover.Action.RELEASE_CLAIM,
                                     target="w:checkout",
                                     evidence="the supervisor was killed and no descendant survives")
                released = "applied"
            except recover.RecoveryRefused as exc:
                released = f"refused: {exc}"
            stale = any(f.kind is recover.FindingKind.CLAIM_STALE_GENERATION for f in findings)
            observed(
                row,
                alive is False
                and held is not None and held.task_id == "t8"
                and any(f.kind is recover.FindingKind.RUN_WITHOUT_PROCESS for f in findings)
                and stale is False
                and released.startswith("refused:"),
                f"the supervisor launched a check that announced pid {descendant}; after "
                f"the supervisor was killed that descendant was still running: {alive}, "
                f"so the job object took the tree down with its owner. The claim stayed "
                f"reserved for task {held.task_id if held else None!r} at generation "
                f"{held.generation if held else None} and recovery reported "
                f"{[f.kind.value for f in findings]}, so the claim was not called stale "
                f"and a release against it was {released.split(':')[0]}: the claim is "
                f"current, not abandoned",
            )
        finally:
            if pid_file.is_file():
                kill_tree(int(pid_file.read_text(encoding="utf-8").strip()))


# --- row 9: an old owner submits after supersession ------------------------

STALE_OWNER_SUBMITS = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    # The old attempt did the work and computed its verdict at generation 1.
    stale = tasks.compute_readiness(store, "t9", required_check_ids=["totals-behavior"])
    # Meanwhile the task was reassigned.
    tasks.supersede_task(store, "t9")
    refused = ""
    try:
        tasks.record_readiness(store, "t9", stale)
    except Exception as exc:
        refused = type(exc).__name__ + ": " + str(exc)
    emit(computed_at=stale.context["generation"], verdict=stale.readiness,
         refused=refused, stored=tasks.get_task(store, "t9").readiness)
"""

RELEASE_AFTER_REASSIGNMENT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    # Reconciliation is an explicit action with evidence behind it, because the old
    # worker may still be alive. The claim is released only when the task has
    # advanced, and the release deletes the stale generation's row without
    # touching whatever another generation holds.
    before = [(c.resource_key, c.generation) for c in claims.holders(store)]
    findings = [(f.kind.value, f.target, f.actionable) for f in recover.inspect(store).findings]
    try:
        recover.apply_action(store, recover.Action.RELEASE_CLAIM, target="w:checkout",
                             evidence="the attempt was reassigned and no run of it is running")
        reconciled = "applied"
    except Exception as exc:
        reconciled = type(exc).__name__ + ": " + str(exc)
    # The successor still cannot take what the predecessor holds at the old
    # generation, and the predecessor still cannot release it.
    successor = ""
    try:
        claims.acquire(store, "t9", 2, [ResourceSpec("w:checkout", "exclusive")])
        successor = "acquired"
    except Exception as exc:
        successor = str(exc)
    try:
        claims.release(store, "t9", 1)
        released = "accepted"
    except Exception as exc:
        released = type(exc).__name__ + ": " + str(exc)
    emit(before=before, findings=findings, reconciled=reconciled, successor=successor,
         released=released,
         holds=[(c.resource_key, c.generation) for c in claims.holders(store)])
"""


def row_stale_owner_submits() -> None:
    """A verdict computed at generation 1 is refused once the task is at 2."""
    row = _row(9)
    repo = make_repo("supersession")
    _, store = store_for(repo)
    open_task(store, repo, "t9", "d9", required_checks=[PASSING_CHECK_ID])
    claims.acquire(store, "t9", 1, [ResourceSpec("w:checkout", "exclusive"),
                                    ResourceSpec("w:other", "exclusive")])
    run_one(store, repo, task_id="t9", attempt=1)
    before = tasks.compute_readiness(store, "t9", required_check_ids=[PASSING_CHECK_ID])

    stale = child(STALE_OWNER_SUBMITS, {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in stale:
        unestablished(row, f"the stale owner failed: {stale}")
        return
    stored = tasks.get_task(store, "t9")
    # A third process reconciles the claim, and the predecessor then tries to
    # release what it still believes it owns.
    released = child(RELEASE_AFTER_REASSIGNMENT, {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in released:
        unestablished(row, f"the reconciling process failed: {released}")
        return
    checks = {
        "the old owner had a real passing run": before.readiness == "READY"
        and before.gaps == (),
        "it computed READY at generation 1": before.context["generation"] == 1
        and stale["computed_at"] == 1 and stale["verdict"] == "READY",
        "recording it after reassignment was refused":
        stale["refused"].startswith("ConflictError")
        and "refusing to record readiness for a superseded attempt" in stale["refused"],
        "the task stored no verdict": stored.generation == 2 and stored.readiness is None,
        "recovery saw the claim as stale and actionable":
        any(kind == "claim_with_stale_generation" and ok
            for kind, _target, ok in released["findings"]),
        "an evidenced release was applied": released["reconciled"] == "applied",
        "the claim left the old generation": ("w:checkout", 1) not in released["holds"],
        "the predecessor could not release what it no longer owned":
        released["released"].startswith("ConflictError")
        and "cannot release resources held at generation 2" in released["released"],
    }
    observed(
        row,
        all(checks.values()),
        f"the old owner had a real passing run and computed READY at generation "
        f"{before.context['generation']} before the reassignment; after supersession a "
        f"separate OS process was refused with {stale['refused'].split(':')[0]} and the "
        f"task's stored readiness is still {stored.readiness!r} at generation "
        f"{stored.generation}. Recovery then reported {released['findings']}, an "
        f"explicitly evidenced release was {released['reconciled']}; the successor's "
        f"claim was then {released['successor']} and the predecessor's own release was "
        f"refused with {released['released'].split(':')[0]}, leaving "
        f"{sorted(released['holds'])}. "
        f"Unmet: {[name for name, ok in checks.items() if not ok] or 'none'}",
    )


# --- row 10: a changed contract or policy -----------------------------------

REMAKE_MANIFEST = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    path = project.manifest_path
    before = parse_manifest(project, project.runs_root / "probe").digest()
    body = json.loads(path.read_text(encoding="utf-8"))
    body["checks"].append({"id": "newly-required", "command": body["checks"][0]["command"],
                           "timeout_seconds": 60, "required_scenarios": ["empty-cart"],
                           "artifact": "result.json"})
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    after = parse_manifest(project, project.runs_root / "probe").digest()
    emit(before=before, after=after, changed=before != after,
         checks_after=sorted(parse_manifest(
             project, project.runs_root / "probe").checks),
         pinned=tasks.get_task(store, "t10").policy_digest,
         readiness=tasks.compute_readiness(
             store, "t10", required_check_ids=["totals-behavior"]).readiness)
"""


def row_changed_contract_or_policy() -> None:
    """A changed manifest cannot be satisfied by evidence gathered under the old one."""
    row = _row(10)
    repo = make_repo("policy-change", checks=[*example_checks(), SECOND_PASSING_CHECK])
    _, store = store_for(repo)
    # The contract names the mandatory set for this attempt, and the policy digest
    # is pinned when the attempt opens rather than read from the manifest later.
    # The floor names the check this row installs afterwards, which is what makes
    # the row's claim: a required check that has no evidence under the old policy,
    # and evidence under the new one.
    open_task(store, repo, "t10", "policy-v1",
              required_checks=[PASSING_CHECK_ID, NEWLY_REQUIRED_CHECK["id"]])
    # Only the first check is registered, so the second has no evidence at all.
    run_one(store, repo, task_id="t10", attempt=1)
    under_v1 = tasks.compute_readiness(
        store, "t10", required_check_ids=[PASSING_CHECK_ID, "newly-required"])
    baseline_v1 = run_rows(store._db_path)[0]["configuration_digest"]

    changed = child(REMAKE_MANIFEST, {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in changed:
        unestablished(row, f"the policy change failed: {changed}")
        return
    baseline_before_change = changed["before"]
    # Still the old manifest: re-running what is registered reproduces the old
    # configuration digest, which is what makes the old evidence unusable.
    run_one(store, repo, task_id="t10", attempt=1)
    unchanged_digest = run_rows(store._db_path)[0]["configuration_digest"]
    under_still_v1 = tasks.compute_readiness(
        store, "t10", required_check_ids=[PASSING_CHECK_ID, "newly-required"])

    # The new manifest is installed, and the newly required check now runs under
    # it, so the evidence is gathered under the new policy.
    (repo / "verification" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "checks": [*example_checks(), SECOND_PASSING_CHECK,
                                                    NEWLY_REQUIRED_CHECK]}, indent=2),
        encoding="utf-8")
    run_one(store, repo, "newly-required", task_id="t10", attempt=1)
    baseline_v2 = run_rows(store._db_path)[0]["configuration_digest"]
    under_v2 = tasks.compute_readiness(
        store, "t10", required_check_ids=[PASSING_CHECK_ID, "newly-required"])
    observed(
        row,
        under_v1.readiness == "BLOCKED"
        and changed["changed"] is True
        and "newly-required" in changed["checks_after"]
        and changed["pinned"] == "policy-v1"
        and baseline_v1 == baseline_before_change
        and under_still_v1.readiness == "BLOCKED"
        and baseline_v2 != baseline_before_change
        and under_v2.readiness == "READY",
        f"the task opened with a contract requiring "
        f"{sorted(['totals-behavior', 'newly-required'])} and a pinned policy "
        f"{changed['pinned']!r}; with the first check run and the second never run "
        f"the task was {under_v1.readiness} with gaps {list(under_v1.gaps)}. The run "
        f"it made recorded configuration digest {baseline_v1[:12]}..., and after a "
        f"second process appended the missing check the manifest digest moved to "
        f"{changed['after'][:12]}.... A run under the old manifest still recorded "
        f"{unchanged_digest[:12]}... and left the task {under_still_v1.readiness}; only "
        f"a run under the new manifest recorded {baseline_v2[:12]}..., and only that "
        f"run made the task {under_v2.readiness}: evidence gathered under the old "
        f"configuration does not satisfy the new requirement",
    )


# --- row 11: all checks pass but one required check is absent ---------------

SECOND_PASSING_CHECK = {
    "id": "second-behavior",
    "command": [sys.executable, "-c",
                "import json, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps("
                "{'schema_version': 1, 'scenarios': ["
                "{'id': 's1', 'result': 'PASS', 'observation': 'ok'}]}))\n",
                "{{run_dir}}/result.json"],
    "timeout_seconds": 60,
    "required_scenarios": ["s1"],
    "artifact": "result.json",
}


def example_checks() -> list[dict]:
    body = json.loads((EXAMPLE / "verification" / "manifest.json").read_text(encoding="utf-8"))
    return body["checks"]


#: The check a policy change adds. Reusing the example's command keeps the point
#: of the row on the policy rather than on the command.
NEWLY_REQUIRED_CHECK = {
    "id": "newly-required",
    "command": [sys.executable, "-c",
                "import json, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps("
                "{'schema_version': 1, 'scenarios': ["
                "{'id': 's1', 'result': 'PASS', 'observation': 'ok'}]}))\n",
                "{{run_dir}}/result.json"],
    "timeout_seconds": 60,
    "required_scenarios": ["s1"],
    "artifact": "result.json",
}


def row_required_check_absent() -> None:
    """Absent evidence is never success."""
    row = _row(11)
    repo = make_repo("absent-check", checks=[*example_checks(), SECOND_PASSING_CHECK])
    _, store = store_for(repo)
    open_task(store, repo, "t11", "d11", required_checks=[PASSING_CHECK_ID])

    first = run_one(store, repo, task_id="t11", attempt=1)
    second = run_one(store, repo, "second-behavior", task_id="t11", attempt=1)
    both = tasks.compute_readiness(
        store, "t11", required_check_ids=[PASSING_CHECK_ID, "second-behavior"])
    missing = tasks.compute_readiness(
        store, "t11", required_check_ids=[PASSING_CHECK_ID, "second-behavior", "never-run"])
    recorded = tasks.record_readiness(store, "t11", missing)

    observed(
        row,
        first.outcome.to_json()["result"] == "PASS"
        and second.outcome.to_json()["result"] == "PASS"
        and both.readiness == "READY"
        and missing.readiness == "BLOCKED"
        and missing.gaps == ("no completed run for required check 'never-run'",)
        and recorded.readiness == "BLOCKED",
        f"two checks ran and both reported {first.outcome.to_json()['result']} and "
        f"{second.outcome.to_json()['result']}, so the task was {both.readiness} "
        f"against those two; naming a third required check that has no run gave "
        f"{missing.readiness} with gaps {missing.gaps}, and that verdict is what the "
        f"task now records ({recorded.readiness})",
    )


# --- row 12: the client asks for fewer checks than policy requires ----------
#
# The row is driven through `Server.call_tool`, the same entry point an MCP SDK
# adapter forwards to, because the surface that holds the baseline is the tool
# rather than the readiness function. The floor is derived at admission and
# unioned again at the decision, so a client naming a smaller list narrows
# nothing at either point. A row that only called `compute_readiness` would have
# measured the wrong thing, and would have reported a failure that the product
# does not actually have.
#
# The client sends the arguments `task_begin` accepts and nothing else. It named
# neither `policy_digest` nor `checkout_ref`: R1 removed both from the schema
# because a caller-supplied digest let a task bind to a checkout and a policy
# and then be compared against evidence it chose itself (F04). Both are derived
# inside admission now, and this row is also what proves it -- the client cannot
# name a digest because the tool refuses the call, so the digest the task is
# pinned to is the one measured from the policy in force.

SMALLER_SELECTION_CLIENT = """
    import sys
    sys.path.insert(0, {src!r})
    from vkit.mcp import Server
    server = Server({root!r})
    begin = server.call_tool("task_begin", {{
        "contract": {{"required_checks": {asked!r}}},
        "request_id": "req-12",
    }})
    task_id = begin.content["task_id"]
    # The client runs the one check it asked for.
    start = server.call_tool("check_start", {{
        "task_id": task_id, "check_ids": {asked!r}, "request_id": "req-12-run",
    }})
    # And asks to finalize against a selection of one, naming only what it ran.
    small = server.call_tool("task_finalize", {{"task_id": task_id,
                                               "check_ids": {asked!r}}})
    # Read back what that decision actually recorded, before anything else runs.
    from vkit.paths import open_project
    from vkit.storage import Store
    from vkit import tasks as task_module
    stored_after_small = task_module.get_task(
        Store(open_project({root!r}).db_path), task_id).readiness
    # The check the client never asked for has now actually run.
    server.call_tool("check_start", {{"task_id": task_id, "check_ids": {unasked!r},
                                     "request_id": "req-12-run-2"}})
    full = server.call_tool("task_finalize", {{"task_id": task_id}})
    emit(task_id=task_id, floor_at_begin=begin.content["required_checks"],
         pinned_digest=begin.content["policy_digest"],
         ran=[r["check_id"] + "=" + r["result"] for r in start.content["runs"]],
         small=small.content, stored_after_small=stored_after_small, full=full.content)
"""


def row_smaller_check_selection() -> None:
    """A smaller selection cannot produce READY while the baseline is missing."""
    row = _row(12)
    repo = make_repo("smaller-selection", checks=[*example_checks(), SECOND_PASSING_CHECK])
    # What the policy registers is read from the policy, not assumed from the
    # fixture it was built with, so the row can say it asked for a strict subset
    # rather than a list that happens to look smaller.
    project = open_project(repo)
    policy_checks = sorted(parse_manifest(project, project.runs_root / "probe").checks)
    policy_digest = parse_manifest(project, project.runs_root / "probe").digest()
    # The client narrows to a single check, and the policy registers another it
    # never mentions. It asks the same question twice -- at admission, and again
    # when it finalizes naming only what it ran -- so a tool that honoured either
    # request would report a floor of one and the row would fail.
    asked = [PASSING_CHECK_ID]
    unasked = [SECOND_PASSING_CHECK["id"]]

    client = child(SMALLER_SELECTION_CLIENT.format(
        src=str(SRC), root=str(repo), asked=asked, unasked=unasked))
    if "__error__" in client:
        unestablished(row, f"the client failed: {client}")
        return
    small = client["small"]
    full = client["full"]
    unasked_gap = f"no completed run for required check '{SECOND_PASSING_CHECK['id']}'"
    observed(
        row,
        # The selection was genuinely narrower than the floor, at both points.
        policy_checks == sorted([*asked, *unasked])
        and client["floor_at_begin"] == policy_checks
        and client["floor_at_begin"] != asked
        # And the client did not get to choose the digest its evidence is
        # compared against, because the tool no longer accepts one.
        and client["pinned_digest"] == policy_digest
        and client["ran"] == [f"{PASSING_CHECK_ID}=PASS"]
        and small["readiness"] == "BLOCKED"
        and small["gaps"] == [unasked_gap]
        and small["required_checks"] == policy_checks
        and client["stored_after_small"] == "BLOCKED"
        and full["readiness"] == "READY"
        and full["gaps"] == [],
        f"the policy registers {len(policy_checks)} checks {policy_checks} and the "
        f"client asked for only {asked}, naming neither a policy digest nor a "
        f"checkout; the task was still frozen against all of {client['floor_at_begin']}, "
        f"pinned to policy digest {client['pinned_digest'][:12]}... as measured rather "
        f"than supplied. The client ran {client['ran']} only, then called task_finalize "
        f"naming just that one check; the tool refused to narrow the selection and "
        f"computed readiness over {small['required_checks']}, so the answer was "
        f"{small['readiness']} with gaps {small['gaps']} and the task recorded "
        f"{client['stored_after_small']!r}. The mandatory baseline was required "
        f"rather than skipped. Once {SECOND_PASSING_CHECK['id']} actually ran, the "
        f"same task became {full['readiness']} with no gaps",
    )


# --- row 13: a disk or locking failure --------------------------------------
#
# The row asserts the CLAIM and not the exception's class name. "A locking
# failure is diagnosable and fabricates nothing" is a statement about what is in
# the database afterwards, and that is what is asserted: both refusals were
# raised rather than swallowed, no verdict was written, no claim was invented,
# and an outside reader still sees exactly the rows there were before. The class
# a refusal arrives as is an implementation detail R1 already changed once -- it
# wrapped a raw `sqlite3.OperationalError` in `StoreError` (c884de9) -- and this
# row was written before that (7e20fae), so pinning the class bought nothing and
# went stale for free. A row that would fail the next time the wrapping moved is
# a row that measures the wrapping, not the behaviour.
#
# What replaces it is not weaker. The refusals are still required to have been
# RAISED (an empty string means the call succeeded and the row fails), the
# message is still required to name the contended write lock rather than
# something vaguer, and the state assertions are checked over a connection that
# shares nothing with the code under test, which is stricter than the old
# `isinstance(observations, list)`.

def row_disk_or_locking_failure() -> None:
    """A store that cannot take the write lock must not produce a verdict."""
    row = _row(13)
    repo = make_repo("locking-failure")
    _, store = store_for(repo)
    open_task(store, repo, "t13", "d13", required_checks=[PASSING_CHECK_ID])
    run_one(store, repo, task_id="t13", attempt=1)
    before = tasks.compute_readiness(store, "t13", required_check_ids=[PASSING_CHECK_ID])
    # What an outside reader sees before anything is attempted, and again after.
    # A row that only compared the store to itself would prove the store is
    # self-consistent, which is not the question.
    baseline = task_rows(store._db_path)

    # Induce a genuine lock failure: another process holds the write lock. This is
    # induced on the store rather than by making a file read-only, so what is
    # exercised is the contended-lock path and not a permission error.
    holder = sqlite3.connect(store._db_path, isolation_level=None, timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE tasks SET policy_digest = 'held-by-another-process'"
                   " WHERE task_id = 't13'")
    try:
        failure = ""
        try:
            claims.acquire(store, "t13", 1, [ResourceSpec("w:checkout", "exclusive")])
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        # A verdict that was already computed cannot be recorded while locked.
        record_failure = ""
        try:
            tasks.record_readiness(store, "t13", before)
        except Exception as exc:
            record_failure = f"{type(exc).__name__}: {exc}"
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    after = tasks.compute_readiness(store, "t13", required_check_ids=[PASSING_CHECK_ID])
    observations = recover.inspect(store).to_json()["findings"]
    version = store.version()
    survived = task_rows(store._db_path)
    # `readiness` is None rather than `before.readiness`: the failure must have
    # left the stored task exactly as it found it, which is a stricter reading
    # than "the verdict still computes to the same answer".
    recorded_after = tasks.get_task(store, "t13").readiness
    observed(
        row,
        before.readiness == "READY"
        # Both refusals were raised, and both said the write lock was the reason.
        # The class is deliberately not asserted; see the note above this row.
        and bool(failure) and "could not take the write lock" in failure
        and bool(record_failure) and "could not take the write lock" in record_failure
        and after.readiness == "READY"
        # Nothing fabricated: no claim, and the stored row is byte-for-byte what
        # it was, read over a connection that shares nothing with the store.
        and claims.holder(store, "w:checkout") is None
        and recorded_after is None
        and survived == baseline
        and claim_rows(store._db_path) == []
        # And the failure stayed diagnosable rather than becoming a broken file:
        # recovery reports on the store instead of raising, and the schema is intact.
        and version == LATEST_SCHEMA_VERSION
        and isinstance(observations, list),
        f"while another process held the write lock, acquiring a resource raised "
        f"{failure.split(':')[0]} ({failure.split(': ', 1)[1][:52]!r}) and recording a "
        f"readiness raised {record_failure.split(':')[0]}; both refusals named the "
        f"contended write lock. No claim was created, so nothing fabricated ownership: "
        f"claim_holders over a fresh connection holds [], and the task row read outside "
        f"the store is unchanged from before the failure ({survived == baseline}), with "
        f"its stored readiness still {recorded_after!r} rather than the {before.readiness} "
        f"that had been computed. The database stayed readable at schema version "
        f"{version} and recovery reported {len(observations)} finding(s) instead of "
        f"raising, so the failure left the state diagnosable. NOT induced: a full disk, "
        f"a read-only state directory, and a corrupted database, so those remain "
        f"unmeasured",
    )


# --- row 14: stateful operation sequences -----------------------------------

#: The second worker row 14 races. It is deliberately built here rather than
#: inside the child program, because a nested `python -c` cannot use the child's
#: own preamble and would have to repeat every import it needs.
RIVAL_SOURCE = CHILD_PREAMBLE + """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    # The same request, from another process, at the same moment. Exactly one of
    # the two workers can end up owning the resource.
    idempotency.begin(store, request_id="req-14", operation="check.start",
                      payload={"task_id": "t14", "check_id": "totals-behavior"})
    try:
        claims.acquire(store, "t14", 1, [ResourceSpec("w:checkout", "exclusive")])
        claims.acquire(store, "t14-rival", 1, [ResourceSpec("w:shared", "exclusive")])
    except ConflictError:
        pass
"""

STATEFUL_SEQUENCE = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    payload = {{"task_id": "t14", "check_id": "totals-behavior"}}
    run_id = idempotency.begin(store, request_id="req-14", operation="check.start",
                               payload=payload)
    # The client believes the request failed and retries it, in this process.
    for _ in range(3):
        idempotency.begin(store, request_id="req-14", operation="check.start",
                          payload=payload)
    # A second worker is handed the same request while the first is still going.
    # Two processes racing for one claim is exactly the duplicate ownership this
    # row looks for, so neither is told what the other did. Its source is
    # interpolated here because a nested interpreter cannot see this module's
    # globals, and it is passed as text rather than a heredoc for the same reason.
    rival_source = {rival_source!r}
    rival = subprocess.Popen([sys.executable, "-c", rival_source], env=dict(os.environ),
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    # The attempt is reassigned, and the stale readiness is refused while the
    # predecessor's claim is still current and unambiguous.
    stale = tasks.compute_readiness(store, task_id="t14", required_check_ids=["totals-behavior"])
    tasks.supersede_task(store, "t14")
    try:
        tasks.record_readiness(store, "t14", stale)
        stale_outcome = "accepted"
    except Exception as exc:
        stale_outcome = type(exc).__name__ + ": " + str(exc)
    try:
        claims.acquire(store, "t14", 2, [ResourceSpec("w:checkout", "exclusive")])
        successor = "acquired"
    except Exception as exc:
        successor = str(exc)
    rival.wait(timeout=90)
    emit(run_id=run_id, successor=successor,
         rival_error=rival.stderr.read().strip()[-200:],
         stale_outcome=stale_outcome,
         holds=[(c.resource_key, c.generation) for c in claims.holders(store)])
"""

LATER_CLIENT = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    run_id = idempotency.begin(store, request_id="req-14", operation="check.start",
                               payload={"task_id": "t14", "check_id": "totals-behavior"})
    manifest = parse_manifest(project, project.runs_root / "probe")
    if (store.run_dir(run_id) / "report.json").is_file():
        report = store.load(run_id)
        emit(run_id=run_id, replayed=True, result=report["outcome"]["result"])
    else:
        result = run_check(manifest, "totals-behavior", store=store,
                           source=compute_source_identity(project), run_id=run_id)
        store.attach_task(run_id, "t14", tasks.get_task(store, "t14").generation)
        emit(run_id=run_id, replayed=False, result=result.outcome.to_json()["result"])
"""


def row_stateful_sequences() -> None:
    """A sequence of operations leaves one owner and accepts no stale attempt."""
    row = _row(14)
    repo = make_repo("stateful")
    _, store = store_for(repo)
    open_task(store, repo, "t14", "d14", required_checks=[PASSING_CHECK_ID])
    claims.acquire(store, "t14", 1, [ResourceSpec("w:checkout", "exclusive")])
    env = {"ACCEPTANCE02_ROOT": str(repo)}

    first = child(STATEFUL_SEQUENCE.format(rival_source=RIVAL_SOURCE),
                  {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in first:
        unestablished(row, f"the first client failed: {first}")
        return
    # A second process picks the same work up and finishes it.
    second = child(LATER_CLIENT, env)
    if "__error__" in second:
        unestablished(row, f"the second client failed: {second}")
        return
    # A third retries the same request id after the run completed.
    third = child(LATER_CLIENT, env)
    if "__error__" in third:
        unestablished(row, f"the third client failed: {third}")
        return

    runs = run_rows(store._db_path)
    claims_table = claim_rows(store._db_path)
    owners = [c["resource_key"] for c in claims_table]
    duplicated = len(owners) != len(set(owners))
    stored = tasks.get_task(store, "t14")
    # Each condition is named, because a row that fails says which claim about
    # the build turned out to be wrong rather than only that something did.
    checks = {
        "one run id across three clients": (
            first["run_id"] == second["run_id"] == third["run_id"]),
        "the run reached PASS": second["result"] == "PASS" and third["result"] == "PASS",
        "the retry replayed rather than re-executed": third["replayed"] is True,
        "one run row in the table": len(runs) == 1,
        "no duplicate owner of the resource": not duplicated and len(claims_table) == 1,
        "the stale readiness was refused": first["stale_outcome"].startswith("ConflictError"),
        "no stale attempt was accepted": stored.readiness is None,
    }
    observed(
        row,
        all(checks.values())
        # Measured behaviour of this build, not the ideal, and stated here rather
        # than asserted as correct: a successor cannot take a resource the
        # predecessor still holds, and recovering it is an explicit release with
        # evidence rather than something supersession does by itself.
        and first["successor"].startswith("resource 'w:checkout' is already held by task 't14'")
        and first["rival_error"] == ""
        and stored.generation == 2
        and claims_table[0]["generation"] == 1,
        f"three OS processes, one of which launched a fourth, interleaved four begin "
        f"calls, a racing claim on the same resource, a reassignment, a superseded "
        f"readiness, a run that completed in the second process, and a retry by the "
        f"third: all three named run {first['run_id'][:12]}... and the runs table "
        f"holds {len(runs)} row, so the retry after completion did not execute again. "
        f"Unmet: {[name for name, ok in checks.items() if not ok] or 'none'}. One "
        f"limit of this build is measured and not asserted correct: the successor's "
        f"claim was refused ({first['successor']}), so reassignment left the "
        f"predecessor holding w:checkout at generation 1, which is the conservative "
        f"direction and means recovering it is an explicit evidenced release",
    )


# --- driving the table ------------------------------------------------------

ROW_FUNCTIONS = {
    1: row_100_competing_claims,
    2: row_disjoint_resource_sets,
    3: row_multi_resource_conflict,
    4: row_transaction_owner_killed,
    5: row_start_retried_around_disconnect,
    6: row_parent_exits,
    7: row_cancel_repeated,
    8: row_supervisor_dies,
    9: row_stale_owner_submits,
    10: row_changed_contract_or_policy,
    11: row_required_check_absent,
    12: row_smaller_check_selection,
    13: row_disk_or_locking_failure,
    14: row_stateful_sequences,
}


def run_rows_in_order(numbers: list[int]) -> None:
    """Run each row function and time it, leaving the verdict in RESULTS."""
    for number in numbers:
        started = time.monotonic()
        try:
            ROW_FUNCTIONS[number]()
        except Exception as exc:  # noqa: BLE001 - a broken row is a FAIL, not a crash
            row = _row(number)
            row.verdict = FAIL
            row.note = f"raised {type(exc).__name__}: {exc}"
        row = _row(number)
        if row.verdict == UNREACHED:
            row.note = "the row function returned without writing a verdict"
        row.note = f"{row.note} [{time.monotonic() - started:.1f}s]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk the Plan 02 acceptance table.")
    parser.add_argument("--only", type=int, nargs="+", choices=sorted(ROW_FUNCTIONS),
                        help="run only these acceptance rows")
    args = parser.parse_args(argv)

    numbers = args.only or sorted(ROW_FUNCTIONS)
    RESULTS.clear()
    for number in numbers:
        _row(number)
    run_rows_in_order(numbers)
    rows = [row for row in RESULTS if row.number in set(numbers)]

    passed = sum(1 for row in rows if row.verdict == PASS)
    failed = [row for row in rows if row.verdict == FAIL]
    skipped = [row for row in rows if row.verdict == SKIP]
    unreached = [row for row in rows if row.verdict == UNREACHED]
    print(f"\n{passed}/{len(rows)} acceptance rows pass "
          f"({len(skipped)} skipped, {len(failed)} failed, {len(unreached)} never ran)")
    for row in rows:
        print(f"[{row.verdict:^6}] row {row.number}: {ROW_TITLES[row.number]}")
        print(f"         {row.note}")
    for label, group in (("SKIPPED", skipped), ("FAILED", failed), ("NEVER RAN", unreached)):
        if group:
            print(f"{label}:")
            for row in group:
                print(f"  - row {row.number} ({ROW_TITLES[row.number]}): {row.note}")
    return 0 if not failed and not unreached else 1


if __name__ == "__main__":
    sys.exit(main())
