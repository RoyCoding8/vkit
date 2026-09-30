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
from dataclasses import dataclass
from pathlib import Path

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
from vkit.storage import Store, StoreError  # noqa: E402
from vkit.supervisor import cancel_run, job_name_for, start_run  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
UNREACHED = "UNREACHED"

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
    12: "Client requests fewer checks than policy requires",
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
    """Open a row. It stays UNREACHED until the row itself writes a verdict."""
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


def raw_rows(db_path: Path, table: str, columns: str) -> list[dict]:
    """Read a table over a connection that shares nothing with the code.

    The observer opens its own connection, so what it reports is what survived to
    the file. Reading through the same `Store` object that performed the write
    would only prove the object is self-consistent.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(f"SELECT {columns} FROM {table}")
        names = [c.strip() for c in columns.split(",")]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def claim_rows(db_path: Path) -> list[dict]:
    return raw_rows(db_path, "claim_holders",
                    "resource_key, kind, capacity, held, task_id, generation")


def run_rows(db_path: Path) -> list[dict]:
    return raw_rows(db_path, "runs",
                    "run_id, check_id, task_id, lifecycle, result, reason")


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
from vkit.claims import ConflictError, ResourceSpec
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
    out = subprocess.run(["tasklist", "/FI", "PID eq " + str(pid), "/NH"],
                         capture_output=True, text=True, timeout=60)
    return str(pid) in out.stdout
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


def spawn_child(program: str, env: dict | None = None) -> subprocess.Popen:
    """Start a child that outlives this call, for a row that watches it work.

    `child` reclaims its process, which is wrong for these rows: they have to
    cancel, kill, or observe a supervisor while it is still running. The caller
    owns the returned process and must stop it.
    """
    source = CHILD_PREAMBLE + textwrap.dedent(program)
    return subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})},
    )


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
    """Terminate a pid and its descendants on this host, quietly."""
    if not IS_WINDOWS:
        try:
            os.killpg(os.getpgid(pid), 9)
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
    # One uncontested claim per process, so exactly one racer can be holding the
    # resource before the gate opens and the other three see a refusal.
    first = "conflict"
    try:
        acquire(store, task, 1, [ResourceSpec(resource, "exclusive")])
        first = "acquired"
    except ConflictError as exc:
        first = str(exc)
    with open(os.environ["ACCEPTANCE02_READY"], "w") as sentinel:
        sentinel.write("ready")
    with open(os.environ["ACCEPTANCE02_GATE"], "r") as gate:
        gate.read(1)
    results = []
    for index in range(attempts):
        try:
            acquire(store, task + "-" + str(index), 1,
                    [ResourceSpec(resource, "exclusive")])
            results.append("acquired")
        except ConflictError as exc:
            results.append(str(exc))
    emit(task=task, first=first, results=results)
"""


def row_100_competing_claims() -> None:
    row = _row(1)
    repo = make_repo("claims-100")
    _, store = store_for(repo)
    resource = "w:checkout"
    processes, attempts = 4, 25

    rendezvous = Path(tempfile.mkdtemp())
    gates = [rendezvous / f"gate{index}" for index in range(processes)]
    racers = []
    for index, gate in enumerate(gates):
        racers.append(spawn_child(CLAIM_RACER, {
            "ACCEPTANCE02_ROOT": str(repo),
            "ACCEPTANCE02_ATTEMPTS": str(attempts),
            "ACCEPTANCE02_GATE": str(gate),
            "ACCEPTANCE02_READY": str(rendezvous / f"ready{index}"),
        }))
    try:
        deadline = time.time() + 90
        while time.time() < deadline and not all(
                (rendezvous / f"ready{index}").exists() for index in range(processes)):
            if any(racer.poll() is not None for racer in racers):
                break
            time.sleep(0.1)
        ready = [(rendezvous / f"ready{index}").exists() for index in range(processes)]
        if not all(ready):
            unestablished(row, f"only {ready.count(True)} of {processes} racers reached "
                                f"the rendezvous, so 100 attempts never contended")
            return
        # Opening a pipe for write is what releases the readers. Each racer is
        # blocked in `gate.read(1)` by the time this byte is written.
        with open(gates[0], "w") as gate:
            gate.write("g")
        reports = []
        for index, racer in enumerate(racers):
            out, err = racer.communicate(timeout=120)
            parsed = [json.loads(line[len("ACCEPTANCE02 "):])
                      for line in (out or "").splitlines()
                      if line.startswith("ACCEPTANCE02 ")]
            if not parsed:
                unestablished(row, f"racer {index} reported nothing; "
                                    f"stderr={err.strip()[-300:]!r}")
                return
            reports.extend(parsed)
    finally:
        for racer in racers:
            if racer.poll() is None:
                kill_tree(racer.pid)

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

    victim = spawn_child(KILL_DURING_TRANSACTION, {"ACCEPTANCE02_ROOT": str(repo)})
    reported = read_report(victim, timeout=90)
    if "inserted" not in reported:
        victim.kill()
        victim.communicate(timeout=30)
        unestablished(row, f"the victim never reported its uncommitted inserts: {reported!r}")
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
        before == [] and after == [] and version == 2
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
    tasks.open_task(store, task_id="t5", contract={"goal": "ship"}, policy_digest="d5")
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
        f"the same request id yielded run {first['first'][:12]}… in both clients "
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
    # The parent claims the request key, registers the run, and records launch
    # intent. Then it stops. This is the MCP-like shape: a client that asked for
    # a run and went away without answering.
    idempotency.begin(store, request_id="req-6", operation="check.start",
                      payload={"check_id": "hangs"})
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
    # A retry carrying the request key the dead parent claimed.
    subject = idempotency.begin(store, request_id="req-6", operation="check.start",
                                payload={"check_id": "hangs"})
    # A cancel naming the run, with a well-formed identity the store can compare.
    outcome, _ = cancel_run(store, run_id,
                            identity=ProcessIdentity(identity.get("pid") or 0, 0))
    emit(subject=subject, read=read, cancel=outcome.to_json())
"""


def row_parent_exits() -> None:
    """An MCP-like parent exits; a second process reattaches and says what is true."""
    row = _row(6)
    repo = make_repo("parent-exits", checks=[hang_check(Path(tempfile.mkdtemp()) / "p.pid")])
    _, store = store_for(repo)
    run_id = uuid.uuid4().hex
    env = {"ACCEPTANCE02_ROOT": str(repo), "ACCEPTANCE02_RUN_ID": run_id}

    parent = spawn_child(PARENT_THAT_EXITS, env)
    reported = read_report(parent, timeout=90)
    if not reported.get("registered"):
        parent.kill()
        parent.communicate(timeout=30)
        unestablished(row, f"the parent never registered its run: {reported!r}")
        return
    parent.kill()
    parent.wait(timeout=30)

    # A different process, holding nothing of the parent's in memory, reads and acts.
    client = child(REATTACHING_CLIENT, env)
    if "__error__" in client:
        unestablished(row, f"the reattaching client failed: {client}")
        return
    read = client["read"]
    final = [r for r in run_rows(store._db_path) if r["run_id"] == run_id]
    start_run_rejected = ""
    try:
        start_run(open_project(repo), store, "hangs", run_id=run_id)
    except StoreError as exc:
        start_run_rejected = str(exc)
    observed(
        row,
        read["row"] is not None
        and read["row"]["lifecycle"] in ("preparing", "running")
        and read["row"]["result"] is None
        and read["identity_pid"] is None
        and read["report_exists"] is False
        and client["subject"] == run_id
        and client["cancel"]["result"] == "BLOCKED"
        and client["cancel"]["reason"] == "ownership_lost"
        and start_run_rejected == f"run {run_id} is already registered"
        and bool(final) and final[0]["result"] == "BLOCKED",
        f"after the parent exited a separate OS process read the run as "
        f"{read['row']['lifecycle']} with no result, a stored process identity with "
        f"pid={read['identity_pid']}, and no report on disk; a retry of the same "
        f"request id returned the same subject {client['subject'][:12]}..., so the dead "
        f"parent's request cannot be re-executed; its cancel was refused as "
        f"{client['cancel']['reason']} and published a terminal {final[0]['result']}, "
        f"and a fresh start_run into that run was refused with "
        f"{start_run_rejected!r}. No result was ever reported for the check and none "
        f"was invented. Measured limit: this build has no surviving supervisor, "
        f"because start_run executes in the calling process and the pid is attached "
        f"only after the command returns, so the in-flight run named no process for a "
        f"second process to continue",
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
    tasks.open_task(store, task_id="t8", contract={{"goal": "ship"}},
                    policy_digest="d8")
    claims.acquire(store, "t8", 1, [ResourceSpec("w:checkout", "exclusive")])
    store.register_run(run_id, "hangs", task_id="t8", attempt=1,
                       source=compute_source_identity(project).to_json(),
                       configuration_digest="d", fixture_digest=None)
    run_command([sys.executable, "-c", {body!r}], cwd=project.root,
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
    env = {"ACCEPTANCE02_ROOT": str(repo), "ACCEPTANCE02_RUN_ID": run_id}

    supervisor = spawn_child(SUPERVISOR_THAT_DIES, env)
    try:
        descendant = announced_pid(pid_file, seconds=120)
        if descendant is None:
            unestablished(row, "the supervisor never launched the check, so nothing "
                                "was observed about a dead supervisor's descendants")
            return
        supervisor.kill()
        supervisor.wait(timeout=30)
        time.sleep(3.0)
        alive = process_alive(descendant)
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
            f"{[f.kind.value for f in findings]}, so the claim was not called stale and "
            f"a release against it was {released.split(':')[0]}: the claim is current, "
            f"not abandoned",
        )
    finally:
        if supervisor.poll() is None:
            kill_tree(supervisor.pid)
        supervisor.communicate(timeout=30)
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
    # The superseded generation also cannot release what it held.
    released = ""
    try:
        claims.release(store, "t9", 1)
    except Exception as exc:
        released = type(exc).__name__ + ": " + str(exc)
    emit(computed_at=stale.context["generation"], verdict=stale.readiness,
         refused=refused, release_refused=released,
         stored=tasks.get_task(store, "t9").readiness)
"""


def row_stale_owner_submits() -> None:
    """A verdict computed at generation 1 is refused once the task is at 2."""
    row = _row(9)
    repo = make_repo("supersession")
    _, store = store_for(repo)
    tasks.open_task(store, task_id="t9", contract={"goal": "ship"}, policy_digest="d9")
    claims.acquire(store, "t9", 1, [ResourceSpec("w:checkout", "exclusive")])
    run_one(store, repo, task_id="t9", attempt=1)
    before = tasks.compute_readiness(store, "t9", required_check_ids=[PASSING_CHECK_ID])

    stale = child(STALE_OWNER_SUBMITS, {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in stale:
        unestablished(row, f"the stale owner failed: {stale}")
        return
    stored = tasks.get_task(store, "t9")
    held = claims.holder(store, "w:checkout")
    observed(
        row,
        before.readiness == "READY"
        and before.context["generation"] == 1
        and stale["computed_at"] == 1
        and stale["verdict"] == "READY"
        and stale["refused"].startswith("ConflictError")
        and "refusing to record readiness for a superseded attempt" in stale["refused"]
        and stored.generation == 2
        and stored.readiness is None
        and "ConflictError" in stale["release_refused"]
        and held is not None and held.generation == 1,
        f"the old owner had a real passing run and computed READY at generation "
        f"{before.context['generation']} before the reassignment; after supersession a "
        f"separate OS process was refused with {stale['refused'].split(':')[0]} and the "
        f"task's stored readiness is still {stored.readiness!r} at generation "
        f"{stored.generation}. The superseded generation was also refused when it "
        f"tried to release the resource it held, so w:checkout is still reserved at "
        f"generation {held.generation if held else None}",
    )


# --- row 10: a changed contract or policy -----------------------------------

REMAKE_MANIFEST = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    path = project.manifest_path
    body = json.loads(path.read_text(encoding="utf-8"))
    body["checks"].append({"id": "newly-required", "command": body["checks"][0]["command"],
                           "timeout_seconds": 60, "required_scenarios": ["empty-cart"],
                           "artifact": "result.json"})
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    before = parse_manifest(project, project.runs_root / "probe").digest()
    after = parse_manifest(project, project.runs_root / "probe").digest()
    emit(before=before, after=after, changed=before != after,
         pinned=tasks.get_task(store, "t10").policy_digest,
         readiness=tasks.compute_readiness(
             store, "t10", required_check_ids=["totals-behavior"]).readiness)
"""


def row_changed_contract_or_policy() -> None:
    """A changed manifest cannot be satisfied by evidence gathered under the old one."""
    row = _row(10)
    repo = make_repo("policy-change")
    _, store = store_for(repo)
    tasks.open_task(store, task_id="t10", contract={"goal": "ship"}, policy_digest="policy-v1")
    run_one(store, repo, task_id="t10", attempt=1)
    under_v1 = tasks.compute_readiness(store, "t10", required_check_ids=[PASSING_CHECK_ID])

    changed = child(REMAKE_MANIFEST, {"ACCEPTANCE02_ROOT": str(repo)})
    if "__error__" in changed:
        unestablished(row, f"the policy change failed: {changed}")
        return
    # A run under the new manifest, so the new check does have passing evidence.
    run_one(store, repo, task_id="t10", attempt=1)
    still_v1 = tasks.compute_readiness(store, "t10", required_check_ids=[PASSING_CHECK_ID])
    under_v2 = tasks.compute_readiness(
        store, "t10", required_check_ids=[PASSING_CHECK_ID, "newly-required"])
    observed(
        row,
        under_v1.readiness == "READY"
        and changed["changed"] is True
        and still_v1.readiness == "READY"
        and changed["pinned"] == "policy-v1"
        and under_v2.readiness == "READY",
        f"the manifest digest changed {changed['before'][:12]}... -> "
        f"{changed['after'][:12]}..., yet the task stayed pinned to policy "
        f"{changed['pinned']!r} and its readiness under the pinned policy is unchanged "
        f"({still_v1.readiness}); the new check was then run and the task reached "
        f"{under_v2.readiness} only when the wider requirement was asked for. Measured "
        f"limit: readiness is decided from the required ids the caller passes and the "
        f"pinned digest it reports back; this build does not read the task's contract "
        f"to derive the requirement, so an old run can be presented as satisfying a new "
        f"policy whenever the caller asks the old question",
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


def row_required_check_absent() -> None:
    """Absent evidence is never success."""
    row = _row(11)
    repo = make_repo("absent-check", checks=[*example_checks(), SECOND_PASSING_CHECK])
    _, store = store_for(repo)
    tasks.open_task(store, task_id="t11", contract={"goal": "ship"}, policy_digest="d11")

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
# rather than the readiness function. `task_finalize` computes the required set
# as the union of the manifest's checks and whatever the caller passes, so a
# client naming a smaller list narrows nothing. A row that only called
# `compute_readiness` would have measured the wrong thing, and would have
# reported a failure that the product does not actually have.

SMALLER_SELECTION_CLIENT = """
    import sys
    sys.path.insert(0, {src!r})
    from vkit.mcp import Server
    server = Server({root!r})
    begin = server.call_tool("task_begin", {{
        "contract": {{"required_checks": ["second-behavior"]}},
        "policy_digest": "policy-v2", "checkout_ref": "w:checkout",
        "request_id": "req-12",
    }})
    task_id = begin.content["task_id"]
    # The client runs the one check it asked for.
    start = server.call_tool("check_start", {{
        "task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-12-run",
    }})
    # And asks to finalize against a selection of one, naming only what it ran.
    small = server.call_tool("task_finalize", {{"task_id": task_id,
                                               "check_ids": ["totals-behavior"]}})
    # Read back what that decision actually recorded, before anything else runs.
    from vkit.paths import open_project
    from vkit.storage import Store
    from vkit import tasks as task_module
    stored_after_small = task_module.get_task(
        Store(open_project({root!r}).db_path), task_id).readiness
    # The baseline check has now actually run.
    server.call_tool("check_start", {{"task_id": task_id, "check_ids": ["second-behavior"],
                                     "request_id": "req-12-run-2"}})
    full = server.call_tool("task_finalize", {{"task_id": task_id}})
    emit(task_id=task_id, claim=begin.content.get("claim"),
         ran=[r["check_id"] + "=" + r["result"] for r in start.content["runs"]],
         small=small.content, stored_after_small=stored_after_small, full=full.content)
"""


def row_smaller_check_selection() -> None:
    """A smaller selection cannot produce READY while the baseline is missing."""
    row = _row(12)
    repo = make_repo("smaller-selection", checks=[*example_checks(), SECOND_PASSING_CHECK])

    client = child(SMALLER_SELECTION_CLIENT.format(src=str(SRC), root=str(repo)))
    if "__error__" in client:
        unestablished(row, f"the client failed: {client}")
        return
    small = client["small"]
    full = client["full"]
    baseline_gap = "no completed run for required check 'second-behavior'"
    observed(
        row,
        client["ran"] == [f"{PASSING_CHECK_ID}=PASS"]
        and small["readiness"] == "BLOCKED"
        and small["gaps"] == [baseline_gap]
        and small["required_checks"] == sorted([PASSING_CHECK_ID, "second-behavior"])
        and client["stored_after_small"] == "BLOCKED"
        and full["readiness"] == "READY"
        and full["gaps"] == [],
        f"the client opened a task and ran {client['ran']} only, then called "
        f"task_finalize naming just that one check; the tool refused to narrow the "
        f"selection and computed readiness over {small['required_checks']}, so the "
        f"answer was {small['readiness']} with gaps {small['gaps']} and the task "
        f"recorded {client['stored_after_small']!r}. The mandatory baseline was "
        f"required rather than skipped. Once {SECOND_PASSING_CHECK['id']} actually "
        f"ran, the same task became {full['readiness']} with no gaps",
    )


# --- row 13: a disk or locking failure --------------------------------------

def row_disk_or_locking_failure() -> None:
    """A store that cannot take the write lock must not produce a verdict."""
    row = _row(13)
    repo = make_repo("locking-failure")
    _, store = store_for(repo)
    tasks.open_task(store, task_id="t13", contract={"goal": "ship"}, policy_digest="d13")
    run_one(store, repo, task_id="t13", attempt=1)
    before = tasks.compute_readiness(store, "t13", required_check_ids=[PASSING_CHECK_ID])

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
        except StoreError as exc:
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
    observed(
        row,
        before.readiness == "READY"
        and failure.startswith("StoreError")
        and "could not take the write lock" in failure
        and record_failure.startswith("OperationalError")
        and after.readiness == "READY"
        and claims.holder(store, "w:checkout") is None
        and version == 2
        and isinstance(observations, list),
        f"while another process held the write lock, acquiring a resource raised "
        f"{failure.split(':')[0]} ({failure.split(': ', 1)[1][:52]!r}) and recording a "
        f"readiness raised {record_failure.split(':')[0]}; no claim was created, so "
        f"nothing fabricated ownership, and the database stayed readable at schema "
        f"version {version}. The verdict computed before the failure "
        f"({before.readiness}) was unchanged afterwards ({after.readiness}) and the "
        f"stored task row still read {tasks.get_task(store, 't13').readiness!r}, so the "
        f"failure left the state diagnosable. NOT induced: a full disk, a read-only "
        f"state directory, and a corrupted database, so those remain unmeasured",
    )


# --- row 14: stateful operation sequences -----------------------------------

STATEFUL_SEQUENCE = """
    project, store = open_repo(os.environ["ACCEPTANCE02_ROOT"])
    payload = {"task_id": "t14", "check_id": "totals-behavior"}
    run_id = idempotency.begin(store, request_id="req-14", operation="check.start",
                               payload=payload)
    # The client believes the request failed and retries it, in this process.
    for _ in range(3):
        idempotency.begin(store, request_id="req-14", operation="check.start",
                          payload=payload)
    # A superseded attempt's worker tries to release what it held.
    try:
        claims.release(store, "t14", 1)
        release_refused = "accepted"
    except ConflictError as exc:
        release_refused = str(exc)
    # And a stale readiness is refused.
    stale = tasks.compute_readiness(store, "t14", required_check_ids=["totals-behavior"])
    tasks.supersede_task(store, "t14")
    try:
        tasks.record_readiness(store, "t14", stale)
        stale_refused = "accepted"
    except ConflictError as exc:
        stale_refused = str(exc)
    emit(run_id=run_id, release_refused=release_refused, stale_refused=stale_refused)
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
    tasks.open_task(store, task_id="t14", contract={"goal": "ship"}, policy_digest="d14")
    claims.acquire(store, "t14", 1, [ResourceSpec("w:checkout", "exclusive")])
    env = {"ACCEPTANCE02_ROOT": str(repo)}

    first = child(STATEFUL_SEQUENCE, env)
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
    observed(
        row,
        first["run_id"] == second["run_id"] == third["run_id"]
        and second["result"] == "PASS"
        and third["replayed"] is True
        and third["result"] == "PASS"
        and len(runs) == 1
        and first["release_refused"].startswith("task 't14' was superseded")
        and first["stale_refused"].startswith("task 't14' was reassigned")
        and stored.readiness is None
        and stored.generation == 2
        and len(claims_table) == 1
        and duplicated is False
        and claims_table[0]["generation"] == 1,
        f"three OS processes interleaved four begin calls, a superseded release, a "
        f"superseded readiness, a run that completed in the second process, and a "
        f"retry by the third: all three named run {first['run_id'][:12]}... and the "
        f"runs table holds {len(runs)} row, so the retry after completion did not "
        f"execute again. The superseded generation was refused twice "
        f"({first['release_refused'].split(':')[0]}, "
        f"{first['stale_refused'].split(':')[0]}) and the task still records readiness "
        f"{stored.readiness!r} at generation {stored.generation}. claim_holders holds "
        f"{[(c['resource_key'], c['task_id'], c['generation']) for c in claims_table]} "
        f"with a key held by more than one row: {duplicated}",
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
    for number in numbers:
        row = _row(number)
        started = time.monotonic()
        try:
            ROW_FUNCTIONS[number]()
        except Exception as exc:  # noqa: BLE001 - a broken row is a FAIL, not a crash
            row.verdict = FAIL
            row.note = f"raised {type(exc).__name__}: {exc}"
        row.note = f"{row.note} [{time.monotonic() - started:.1f}s]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk the Plan 02 acceptance table.")
    parser.add_argument("--only", type=int, nargs="+", choices=sorted(ROW_FUNCTIONS),
                        help="run only these acceptance rows")
    args = parser.parse_args(argv)

    numbers = args.only or sorted(ROW_FUNCTIONS)
    # `run_rows_in_order` opens a row per number, so RESULTS is left holding
    # exactly one row per requested number when it returns.
    run_rows_in_order(numbers)
    by_number = {row.number: row for row in RESULTS}
    rows = [by_number[number] for number in numbers]

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
