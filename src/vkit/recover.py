"""Crash recovery, in two halves: what the records and processes actually show,
and the one action a caller may take about it.

`inspect` reads. It opens no write transaction, updates nothing, and derives every
finding from a row that exists and a process handle it opened itself. `apply_action`
is the only way anything changes, and it takes the target and the evidence as
arguments because neither can be inferred safely.

The asymmetry that shapes the module: a wrong "dead" verdict reassigns a live
worker's writable resource, and a wrong "alive" verdict only leaves something for a
human to look at. Every error case below therefore resolves toward `UNCERTAIN`
unless the operating system positively proved the pid is gone, and every action
that would release a resource refuses unless death was confirmed.

Two things this module deliberately does not do.

**It never reads a timestamp.** `registered_at` is not in the query below, so
elapsed time cannot reach a decision even by accident. An old run with a live
process is live, and an old run with a confirmed-dead process is recoverable, and
nothing about how long ago either started changes that.

**It never invents an outcome.** Marking a dead run for reconciliation records what
the handle reported; it does not publish a report. A run whose process died before
producing an artifact has no evidence, and `storage.publish` is right to refuse it.

PID reuse is the honest limit here. A recorded pid that has since been reused by an
unrelated process reads as alive, so recovery refuses and the claim is preserved.
That is the safe direction: it leaves a record a human can reconcile, rather than
releasing a resource out from under a stranger. The converse cannot happen, because
releasing is only ever done against a confirmed-dead pid and a named claim row.
"""
from __future__ import annotations

import enum
import json
import os
import sqlite3
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone

from .procidentity import CannotConfirm, openprocess_failure_is_gone, process_group_is_alive
from .procs import POSIX_OWNERSHIP
from .storage import REPORT_NAME, Store

if sys.platform == "win32":
    import pywintypes
    import win32api
    import win32con
    import win32process

# Written into the run's process record when a dead run is reconciled. Nesting
# keeps it beside the pid it describes instead of adding a second place that
# claims to know whether a process is alive.
RECONCILIATION_KEY = "reconciliation"
DEAD_CONFIRMED = "process_dead_confirmed"
# Distinct from DEAD_CONFIRMED because it decides a different question. A dead
# process is settled by asking about a pid; an unresolved launch has no pid to ask
# about at all, and is settled by asking about the job and the supervisor. An
# operator reading a record has to be able to see which evidence was gathered.
LAUNCH_ABANDONED = "launch_abandoned"

# Windows reports "this process has not exited yet" as its exit code. A process
# that really did exit with 259 is indistinguishable from one still running; the
# API defines no other signal, and this is the API's limit, not a choice here.
STILL_ACTIVE = 259


class RecoveryRefused(Exception):
    """The requested recovery action was refused, with the reason it was unsafe.

    Separate from a store failure: a refusal is the correct answer to a dangerous
    request, and the caller shows it to the user rather than retrying.
    """


class LivenessState(str, enum.Enum):
    """What an opened process handle proved.

    `UNCERTAIN` is not a hedge. It is the state of a pid this session may not
    open, and it is the state every unrecognized failure falls back to.
    """

    ALIVE = "alive"
    DEAD = "dead"
    UNCERTAIN = "uncertain"


class FindingKind(str, enum.Enum):
    """One way the records and the processes disagree, or one way they cannot."""

    # A report, not a guard. An unfinished run that records no pid is worth
    # showing an operator, and it carries no action. It is NOT the finding that
    # decides a claim release; that one is RUN_UNRESOLVED_LAUNCH below, and
    # conflating the two is what let a claim be released while a live check was
    # still writing.
    RUN_WITHOUT_PROCESS = "run_without_process"
    RUN_DEAD_PROCESS = "run_with_dead_process"
    RUN_LIVE_PROCESS = "run_with_live_process"
    RUN_UNCERTAIN_PROCESS = "run_with_uncertain_process"
    RUN_TERMINAL_LIVE_PROCESS = "terminal_run_with_live_process"
    RUN_TERMINAL_NO_REPORT = "terminal_run_without_report"
    # A launch was attempted and its outcome was never established. Distinct from
    # RUN_WITHOUT_PROCESS because it is actionable: it names the one action that
    # can clear it, and that action is not MARK_RUN_DEAD because deciding it
    # requires the job-name evidence a dead-pid check does not have.
    RUN_UNRESOLVED_LAUNCH = "run_with_unresolved_launch"
    CLAIM_STALE_GENERATION = "claim_with_stale_generation"
    CLAIM_OWNER_MISSING = "claim_with_no_task"


class Action(str, enum.Enum):
    """The recovery actions a caller may name explicitly."""

    RELEASE_CLAIM = "release_claim"
    MARK_RUN_DEAD = "mark_run_dead"
    # Marks an unresolved launch as reconciled. It never deletes anything and
    # never touches a claim; the claim is released by RELEASE_CLAIM, which asks
    # again whether any holder is live. Two explicit actions in order, each with
    # evidence, is what retaining a claim for explicit reconciliation means
    # operationally.
    ABANDON_LAUNCH = "abandon_launch"


@dataclass(frozen=True)
class Liveness:
    """The verdict and the operating-system statement behind it."""

    state: LivenessState
    detail: str
    exit_code: int | None = None

    @property
    def dead(self) -> bool:
        return self.state is LivenessState.DEAD


@dataclass(frozen=True)
class Finding:
    """What is wrong, which record it is wrong about, and what was observed.

    `detail` is the evidence. It quotes record fields and the liveness call, so a
    reviewer can check the conclusion against the observation without rerunning
    anything.
    """

    kind: FindingKind
    target: str
    detail: str
    # The one action this finding could ever support, or None when nothing may
    # be named against it. Putting the action on the finding keeps "what may I
    # do about this" answerable from the report instead of from the reader's
    # knowledge of which target name belongs to which action.
    action: Action | None = None
    liveness: Liveness | None = None
    reconciled: bool = False

    @property
    def actionable(self) -> bool:
        """Whether a recovery action may be named against this finding.

        Alive and uncertain are never actionable, a finding already reconciled is
        not actionable twice, and a finding that names no action is not
        actionable at all.
        """
        if self.action is None or self.reconciled:
            return False
        return self.liveness is None or self.liveness.state is LivenessState.DEAD

    def to_json(self) -> dict:
        document: dict = {
            "kind": self.kind.value,
            "target": self.target,
            "detail": self.detail,
            "actionable": self.actionable,
        }
        if self.action is not None:
            document["action"] = self.action.value
        if self.liveness is not None:
            document["liveness"] = self.liveness.state.value
        if self.reconciled:
            document["reconciled"] = True
        return document


@dataclass(frozen=True)
class Report:
    """The immutable result of one inspection."""

    findings: tuple[Finding, ...] = ()

    def of(self, kind: FindingKind) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.kind is kind)

    def for_target(self, target: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.target == target)

    def to_json(self) -> dict:
        return {"findings": [finding.to_json() for finding in self.findings]}


# --------------------------------------------------------------- liveness


def liveness(
    pid: int | None, *, creation_time: int | None = None, boot_id: str = ""
) -> Liveness:
    """Decide whether a pid is alive, dead, or beyond knowing.

    Getting this backwards kills a live worker, so each error case names its
    direction and the reason for it.

    `creation_time` is the other half of a process identity, and passing it is
    what makes the DEAD answer trustworthy. Windows recycles process
    identifiers, so a pid left behind by an exited process eventually names an
    unrelated one. Judged on the pid alone, that stranger reads DEAD or ALIVE
    with equal confidence, and both are wrong: DEAD releases a run's claims on
    the evidence of a process that never ran the check.

    `boot_id` is the third half on POSIX, where the start time is ticks since
    boot rather than an absolute instant. It is compared because a tick count is
    only meaningful within one boot: WSL restarts the init namespace, so a record
    written before a restart can carry the same tick count as a process running
    after it. It defaults to empty, which is the Windows case, where an absolute
    FILETIME needs no boot to be meaningful.

    So when a creation time is supplied and the live process disagrees, the
    answer is UNCERTAIN, never DEAD. The claim is retained and the run needs
    reconciling, which is the direction that preserves the evidence. A pid that
    no process carries is still DEAD, because then there is no stranger to
    confuse it with and calling it UNCERTAIN would strand every crashed run's
    claims forever.

    Omitting `creation_time` keeps the bare-pid reading. That is correct for a
    record that never stored an identity, and the detail string says so, because
    a weaker answer presented as a full one is how this went wrong once already.

    A pid that is not a pid at all — None, zero, or negative — is UNCERTAIN. Zero
    on POSIX names a process group rather than a process, and treating it as a
    dead worker would release a resource on the strength of a sentinel.

    On Windows, `OpenProcess` decides it:

      * it opens and `GetExitCodeProcess` returns `STILL_ACTIVE`: ALIVE. This is
        the check that must not be skipped. A process that has not exited has no
        exit code, so any comparison other than against `STILL_ACTIVE` reads a
        live worker as dead.
      * it opens and reports any other code: DEAD, with the code as evidence. We
        hold a handle and read a real exit code, which is proof.
      * it fails with ERROR_INVALID_PARAMETER: DEAD. The operating system is saying
        no process carries that pid, which is not the same as declining to tell
        us. That is the only code that means it, so this branch asks
        `procidentity.openprocess_failure_is_gone` rather than reading a table of
        its own.
      * it fails with anything else, including ERROR_ACCESS_DENIED,
        ERROR_INVALID_HANDLE and ERROR_NOT_FOUND: UNCERTAIN. "Cannot open" and
        "does not exist" are different claims and only one of them is a safe
        basis for releasing someone's resource. The first two named here used to
        sit on the DEAD side of this branch, and a DEAD verdict from a code that
        does not establish death is exactly the premise a claim release rests
        on. The default is the direction that preserves the claim.

    On POSIX, `kill(pid, 0)` is a permission-checked existence probe.
    `ProcessLookupError` is DEAD because the kernel said there is no such
    process. `PermissionError` is ALIVE, not uncertain: EPERM from a signal-zero
    probe means the process exists and only the signal was refused, which is
    positive proof. Any other `OSError` is UNCERTAIN.
    """
    if pid is None or pid <= 0:
        return Liveness(
            LivenessState.UNCERTAIN,
            f"pid {pid!r} does not name a single process, so its liveness cannot be established",
        )
    if sys.platform == "win32":
        return _liveness_windows(pid, creation_time)
    return _liveness_posix(pid, creation_time, boot_id)


def _owned_process_liveness(process: dict | None) -> Liveness:
    pid = (process or {}).get("pid")
    if sys.platform != "win32" and (process or {}).get("ownership") == POSIX_OWNERSHIP:
        try:
            alive = process_group_is_alive(pid)
        except CannotConfirm as exc:
            return Liveness(LivenessState.UNCERTAIN, str(exc))
        detail = (
            f"process group {pid} still has members"
            if alive else f"process group {pid} has no members"
        )
        return Liveness(LivenessState.ALIVE if alive else LivenessState.DEAD, detail)
    return liveness(
        pid,
        creation_time=(process or {}).get("creation_time"),
        boot_id=(process or {}).get("boot_id", ""),
    )


def _liveness_windows(pid: int, creation_time: int | None = None) -> Liveness:
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
    except pywintypes.error as exc:
        code = exc.winerror
        if openprocess_failure_is_gone(code):
            return Liveness(
                LivenessState.DEAD,
                f"OpenProcess for pid {pid} failed with Windows error {code}; no process carries that pid",
            )
        return Liveness(
            LivenessState.UNCERTAIN,
            f"OpenProcess for pid {pid} failed with Windows error {code}, which does not prove "
            "death; the pid may name a live process this session may not open, and the claim "
            "stays held",
        )

    try:
        code = win32process.GetExitCodeProcess(handle)
    except pywintypes.error as exc:
        return Liveness(
            LivenessState.UNCERTAIN,
            f"pid {pid} opened but its exit code could not be read ({exc}); liveness is not established",
        )
    finally:
        handle.Close()

    if code == STILL_ACTIVE:
        if creation_time is not None:
            stranger = _creation_time_mismatch(pid, creation_time)
            if stranger is not None:
                return stranger
        return Liveness(
            LivenessState.ALIVE,
            f"pid {pid} opened and reported exit code {code} (STILL_ACTIVE), so it is still running",
            exit_code=code,
        )
    return Liveness(
        LivenessState.DEAD,
        f"pid {pid} opened and reported exit code {code}, so it has exited",
        exit_code=code,
    )


def _creation_time_mismatch(
    pid: int, recorded: int, recorded_boot_id: str = ""
) -> Liveness | None:
    """A Liveness saying the pid is a stranger, or None if the pair matches.

    Reached only when a pid opened and answered, so a mismatch means the OS
    handed this number to a different process after ours exited. That is not
    evidence about our process at all, which is why it is UNCERTAIN rather than
    a verdict on it.

    `recorded_boot_id` is compared as well as the tick count, on POSIX. A start
    time is ticks since boot, so a record written before a restart can carry the
    same tick count as a process running after it, and WSL restarts the whole
    init namespace. Comparing the tick count alone would call that pair the same
    process. It is empty on Windows, where the recorded value is an absolute
    FILETIME and needs no boot to be meaningful, so the check is skipped there.
    """
    from .procidentity import CannotConfirm, read_identity

    try:
        current = read_identity(pid)
    except CannotConfirm:
        return Liveness(
            LivenessState.UNCERTAIN,
            f"pid {pid} is running but its creation time cannot be read, so the "
            "recorded identity cannot be confirmed; the claim is retained",
        )
    if current is None:
        return None
    # The boot id is part of the comparison, not decoration. A start time is
    # ticks since boot, so a record written before a restart can carry the same
    # tick count as a process running after it, and on WSL the whole init
    # namespace restarts together. Comparing the tick count alone would call that
    # pair the same process.
    if current.boot_id and current.boot_id != recorded_boot_id:
        return Liveness(
            LivenessState.UNCERTAIN,
            f"pid {pid} is running under boot {current.boot_id}, not the boot this "
            f"run was recorded in ({recorded_boot_id or 'none'}), so the number was "
            "recycled across a restart and this is a different process; the claim is "
            "retained rather than released on a stranger's evidence",
        )
    if current.creation_time == recorded:
        return None
    return Liveness(
        LivenessState.UNCERTAIN,
        f"pid {pid} is running with creation time {current.creation_time}, not the "
        f"recorded {recorded}, so the number was recycled and this is a different "
        "process; the claim is retained rather than released on a stranger's evidence",
    )

def _liveness_posix(
    pid: int, creation_time: int | None = None, boot_id: str = ""
) -> Liveness:
    """The POSIX probe, plus the identity check the Windows branch performs.

    `kill(pid, 0)` says whether a process exists and nothing about which one, so
    on its own it cannot tell our process from a stranger that inherited the
    number. The identity check is what closes that, and it was missing here:
    `liveness` passed `creation_time` to the Windows branch and dropped it on
    this one, so a run record naming a recycled pid read ALIVE instead of
    UNCERTAIN. Measured before the fix: `liveness(pid)` and
    `liveness(pid, creation_time=133000000000000000)` returned byte-identical
    detail on a live pid.

    The stranger case is UNCERTAIN, not DEAD, and not ALIVE. DEAD would release a
    claim on the evidence of a process that never ran the check; ALIVE would
    report our run as healthy on the strength of someone else's process. The
    claim is retained either way, which is the direction that preserves evidence.
    """
    import errno

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Liveness(LivenessState.DEAD, f"the kernel reports no process with pid {pid}")
    except PermissionError:
        return Liveness(
            LivenessState.ALIVE,
            f"pid {pid} exists and refused the probe with {errno.EPERM}, so it is running",
        )
    except OSError as exc:
        return Liveness(
            LivenessState.UNCERTAIN,
            f"probing pid {pid} failed with {type(exc).__name__}: {exc}, which does not prove death",
        )
    if creation_time is None:
        return Liveness(LivenessState.ALIVE, f"pid {pid} answered the signal-zero probe")
    return _creation_time_mismatch(pid, creation_time, boot_id) or Liveness(
        LivenessState.ALIVE, f"pid {pid} answered the signal-zero probe"
    )


# ---------------------------------------------------------------- reading

# registered_at is absent on purpose. Nothing in this module decides anything by
# how long a record has been sitting there, so the column is never read.
_RUN_COLUMNS = (
    "run_id", "check_id", "task_id", "attempt", "lifecycle", "result",
    "reason", "process_json", "ended_at",
)

_CLAIM_COLUMNS = (
    "resource_key", "kind", "capacity", "held", "task_id", "generation", "acquired_at",
)


@dataclass(frozen=True)
class _Run:
    run_id: str
    check_id: str
    task_id: str | None
    attempt: int | None
    lifecycle: str
    result: str | None
    reason: str | None
    process: dict | None
    ended_at: str | None
    job_name: str | None = None
    abandoned: bool = False

    @property
    def unfinished(self) -> bool:
        """Everything except a run that has published one outcome.

        Was `lifecycle in ("preparing", "running")`, which classified a `launching`
        or `cancelling` run as *finished*. Recovery then reported a mid-flight run
        as terminal-with-no-report, and the guard that decides a claim release was
        never even asked about it. The name already said what this means;
        under-applying it was the defect.
        """
        return self.lifecycle != "terminal"

    @property
    def ownership_known(self) -> bool:
        """Whether this run has published a verified owner.

        False is the dangerous state, not the empty one. It covers a run that is
        `preparing` with no process at all, a run that is `launching` with a
        process whose identity was never published, and a `cancelling` run whose
        termination has not resolved. All three are "something may be running and
        I cannot prove what", and all three must refuse a release.
        """
        return bool((self.process or {}).get("ownership_known"))

    @property
    def job(self) -> str | None:
        """The job name, from either of the two places it is recorded.

        `launches` is written before the process exists and `process_json` after,
        so the launch row is the one that is present for exactly the runs whose
        ownership is least certain. A reader that consulted only `process_json`
        would find no job name for a `launching` run -- the one run that most
        needs a handle on its tree.
        """
        return self.job_name or (self.process or {}).get("job_name")

    @property
    def pid(self) -> int | None:
        pid = (self.process or {}).get("pid")
        return pid if isinstance(pid, int) else None

    @property
    def creation_time(self) -> int | None:
        """The other half of the process identity, when the record carries one.

        A run that never recorded one leaves this None and liveness falls back to
        the bare-pid reading. Every liveness call in this module goes through
        here rather than reaching into the dict, so a record that starts
        carrying an identity is picked up without hunting call sites.
        """
        value = (self.process or {}).get("creation_time")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def boot_id(self) -> str:
        """The boot the recorded identity belongs to, empty on Windows.

        A POSIX start time is ticks since boot, so it is only comparable within
        one boot. WSL restarts the init namespace, so a record written before a
        restart can carry the same tick count as a process running after it. The
        check is skipped when this is empty, which is the Windows case and also a
        record written before the field existed.
        """
        value = (self.process or {}).get("boot_id")
        return value if isinstance(value, str) else ""


def _read_runs(store: Store) -> tuple[_Run, ...]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT " + ", ".join(_RUN_COLUMNS) + " FROM runs ORDER BY rowid"
        ).fetchall()
        # Read in the same transaction as the runs themselves. A launch row
        # written between the two reads would otherwise be missed, and the run it
        # describes is exactly the one whose ownership is least certain.
        launches = {
            row[0]: (row[1], bool(row[2]))
            for row in conn.execute("SELECT run_id, job_name, abandoned FROM launches")
        }
    runs = []
    for row in rows:
        record = dict(zip(_RUN_COLUMNS, row))
        raw = record["process_json"]
        job_name, abandoned = launches.get(record["run_id"], (None, False))
        runs.append(_Run(
            run_id=record["run_id"],
            check_id=record["check_id"],
            task_id=record["task_id"],
            attempt=record["attempt"],
            lifecycle=record["lifecycle"],
            result=record["result"],
            reason=record["reason"],
            process=json.loads(raw) if raw else None,
            ended_at=record["ended_at"],
            job_name=job_name,
            abandoned=abandoned,
        ))
    return tuple(runs)


def _read_claims(store: Store) -> tuple[dict, ...]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT " + ", ".join(_CLAIM_COLUMNS) + " FROM claim_holders ORDER BY resource_key"
        ).fetchall()
    return tuple(dict(zip(_CLAIM_COLUMNS, row)) for row in rows)


def _read_generations(store: Store) -> dict[str, int]:
    with store._connect() as conn:
        rows = conn.execute("SELECT task_id, generation FROM tasks").fetchall()
    return {row[0]: row[1] for row in rows}


def _reconciled(process: dict | None) -> bool:
    return bool((process or {}).get(RECONCILIATION_KEY))


# -------------------------------------------------------------- inspection


def inspect(store: Store) -> Report:
    """Report what the records and the processes present actually show. Changes nothing.

    Every finding carries the record fields it was read from and, where a pid is
    named, the liveness call made against it. A run whose process is alive is
    reported as still running, which is a finding in its own right: it is the
    answer to "is anything still working here", and it is never a licence to
    release what that run holds.
    """
    runs = _read_runs(store)
    claims = _read_claims(store)
    generations = _read_generations(store)
    findings = list(_run_findings(store, runs))
    findings.extend(_claim_findings(claims, generations, runs))
    return Report(tuple(findings))


def _run_findings(store: Store, runs: tuple[_Run, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for run in runs:
        if run.unfinished:
            findings.append(_unfinished_run_finding(store, run))
            continue
        findings.extend(_terminal_run_findings(store, run))
    return findings


def _unfinished_run_finding(store: Store, run: _Run) -> Finding:
    common = (
        f"run {run.run_id!r} for check {run.check_id!r} is recorded {run.lifecycle} "
        f"(task {run.task_id!r}, attempt {run.attempt!r}, result {run.result!r})"
    )
    launch = store.load_launch(run.run_id)
    if run.pid is None or not run.ownership_known:
        if launch is not None and not run.abandoned:
            return Finding(
                kind=FindingKind.RUN_UNRESOLVED_LAUNCH,
                target=run.run_id,
                detail=(
                    f"{common} and a launch was recorded for it, so a process was created "
                    f"and its identity was never published. The job owning its tree is "
                    f"{launch.get('job_name')!r} and the supervisor was pid "
                    f"{launch.get('supervisor_pid')!r}. The claim stays held: an unresolved "
                    "launch is reconciled by naming the evidence that it is over, not by "
                    "inferring from the absent pid that nothing was ever started"
                ),
                action=Action.ABANDON_LAUNCH,
            )
        if launch is not None and run.abandoned:
            return Finding(
                kind=FindingKind.RUN_UNRESOLVED_LAUNCH,
                target=run.run_id,
                detail=(
                    f"{common} and its launch was already abandoned by an operator with "
                    "the evidence they gave. The run itself is not reconciled and holds "
                    "no outcome, so this remains visible; it simply is not the open "
                    "question it was, and the claim may now be released"
                ),
                action=Action.ABANDON_LAUNCH,
                reconciled=True,
            )
        return Finding(
            kind=FindingKind.RUN_WITHOUT_PROCESS,
            target=run.run_id,
            detail=(
                f"{common} and records no process identity, so there is no pid to check. "
                + (
                    "Its launch was already abandoned by an operator, so this is a decided "
                    "state rather than an open one"
                    if run.abandoned else
                    "No launch was ever recorded for it, so nothing was created. The two are "
                    "now distinguishable from the records, and neither is acted on here"
                )
            ),
        )
    state = _owned_process_liveness(run.process)
    if state.state is LivenessState.ALIVE:
        return Finding(
            kind=FindingKind.RUN_LIVE_PROCESS,
            target=run.run_id,
            detail=(
                f"{common} at pid {run.pid}; {state.detail}. This run is still working. "
                "Its resources stay held and no recovery action applies to it."
            ),
            liveness=state,
        )
    if state.state is LivenessState.UNCERTAIN:
        return Finding(
            kind=FindingKind.RUN_UNCERTAIN_PROCESS,
            target=run.run_id,
            detail=(
                f"{common} at pid {run.pid}; {state.detail}. An uncertain process is "
                "presumed alive, so the claim stays held and recovery is needed."
            ),
            liveness=state,
        )
    return Finding(
        kind=FindingKind.RUN_DEAD_PROCESS,
        target=run.run_id,
        detail=(
            f"{common} at pid {run.pid}; {state.detail}. The run stopped mid-flight and "
            "holds no outcome of its own."
        ),
        action=Action.MARK_RUN_DEAD,
        liveness=state,
        reconciled=_reconciled(run.process),
    )


def _terminal_run_findings(store: Store, run: _Run) -> list[Finding]:
    findings: list[Finding] = []
    common = (
        f"run {run.run_id!r} for check {run.check_id!r} is terminal with result "
        f"{run.result!r} and reason {run.reason!r}"
    )
    report_path = store.run_dir(run.run_id) / REPORT_NAME
    if not report_path.is_file():
        findings.append(Finding(
            kind=FindingKind.RUN_TERMINAL_NO_REPORT,
            target=run.run_id,
            detail=(
                f"{common}, but no report exists at {report_path}. This is the window "
                "between claiming the row and swapping the report into place. The run "
                "has no readable evidence, and recovery does not invent one."
            ),
        ))
    if run.process is not None:
        state = _owned_process_liveness(run.process)
        if state.state is LivenessState.ALIVE:
            findings.append(Finding(
                kind=FindingKind.RUN_TERMINAL_LIVE_PROCESS,
                target=run.run_id,
                detail=(
                    f"{common}, yet {state.detail}. A terminal row beside a running "
                    "process means the row was written by something other than the "
                    "process it names."
                ),
                liveness=state,
            ))
        elif state.state is LivenessState.UNCERTAIN:
            findings.append(Finding(
                kind=FindingKind.RUN_TERMINAL_LIVE_PROCESS,
                target=run.run_id,
                detail=(
                    f"{common}, and its process could not be ruled out: {state.detail}"
                ),
                liveness=state,
            ))
    return findings


def _claim_findings(
    claims: tuple[dict, ...],
    generations: dict[str, int],
    runs: tuple[_Run, ...],
) -> list[Finding]:
    findings: list[Finding] = []
    for claim in claims:
        key = claim["resource_key"]
        task_id = claim["task_id"]
        held_generation = claim["generation"]
        state = (
            f"resource {key!r} ({claim['kind']}, {claim['held']} held) is held by task "
            f"{task_id!r} at generation {held_generation}, acquired {claim['acquired_at']}"
        )
        if task_id not in generations:
            findings.append(Finding(
                kind=FindingKind.CLAIM_OWNER_MISSING,
                target=key,
                detail=f"{state}, but no task {task_id!r} exists to own it",
            ))
            continue
        current = generations[task_id]
        if held_generation == current:
            continue
        findings.append(Finding(
            kind=FindingKind.CLAIM_STALE_GENERATION,
            target=key,
            detail=(
                f"{state}, but task {task_id!r} has since advanced to generation {current}. "
                f"{_holder_liveness(runs, task_id, held_generation)}"
            ),
            action=Action.RELEASE_CLAIM,
        ))
    return findings


def _holder_liveness(runs: tuple[_Run, ...], task_id: str, generation: int) -> str:
    """One sentence about whether the stale holder might still be running.

    This is the evidence that decides whether releasing is safe, so it is read
    from the runs rather than from the task, and a pid that cannot be checked is
    reported as uncertain rather than skipped.

    A run with no pid is reported as *unresolved*, not omitted. The old version
    filtered those out and then said "No run of that task has a recorded process
    to check" when the list came back empty, which read as clearance. It is the
    same inference the F13 guard made and it is wrong for the same reason.
    """
    relevant = [run for run in runs if run.task_id == task_id and run.attempt == generation]
    if not relevant:
        return (
            f"No run of {task_id!r} at generation {generation} has a recorded process to check, "
            "so nothing in that generation is known to be writing"
        )
    parts = []
    for run in relevant:
        if run.pid is None or not run.ownership_known:
            parts.append(
                f"run {run.run_id!r} is {run.lifecycle} with no verified owner, so it is "
                "presumed to be running until an operator reconciles it"
            )
            continue
        state = _owned_process_liveness(run.process)
        parts.append(f"run {run.run_id!r} is {run.lifecycle} at pid {run.pid}, {state.state.value}")
    return "; ".join(parts) + "."


# ----------------------------------------------------------------- actions


@contextmanager
def _write(store: Store):
    """One BEGIN IMMEDIATE, committed only if the body returns.

    The write lock is taken before the body's first read, so a decision made
    inside it is made against state that cannot change underneath it. That is why
    the liveness check and the write share a transaction: checked outside, the
    verdict would be about one state of the record and written against another.

    This is the explicit BEGIN/COMMIT idiom that `claims` and `tasks` use, and it
    is used here for the same reason. `Store.transaction` is not: it opens
    BEGIN IMMEDIATE and never commits, and with `isolation_level=None` that
    leaves the lock held and rolls the work back. Using it here would have made
    every successful recovery action a silent no-op.
    """
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def apply_action(store: Store, action: Action, *, target: str, evidence: str) -> Report:
    """Apply one named recovery action and return the inspection that follows.

    `target` and `evidence` are required arguments, not defaults that get filled
    in. A recovery action that names its own subject and shows its own evidence is
    reviewable before it runs; one that goes looking for a likely subject is the
    thing this contract refuses.

    Refusals raise `RecoveryRefused` and change nothing. Returns a fresh `Report`
    so the caller sees the state it produced rather than the state it assumed.
    """
    if not isinstance(action, Action):
        raise RecoveryRefused(f"unknown recovery action {action!r}; recovery never guesses")
    if not isinstance(target, str) or not target.strip():
        raise RecoveryRefused("a recovery action must name the run or resource it affects")
    if not isinstance(evidence, str) or not evidence.strip():
        raise RecoveryRefused(
            f"refusing to {action.value} {target!r}: the evidence permitting the change was empty"
        )

    if action is Action.RELEASE_CLAIM:
        _release_claim(store, target, evidence)
    elif action is Action.ABANDON_LAUNCH:
        _abandon_launch(store, target, evidence)
    else:
        _mark_run_dead(store, target, evidence)
    return inspect(store)


def _abandon_launch(store: Store, run_id: str, evidence: str) -> None:
    """Decide that an unresolved launch is over, on a human's evidence.

    Three things are checked, and all three must hold. The job recorded for the
    run must no longer open, which means no second process can be holding it; the
    supervisor's own pid must be gone, confirmed against the creation FILETIME
    recorded beside it so a recycled pid cannot pass; and there must be no
    published pid whose creation time matches the run's. Two of three is not
    enough, and neither is a job that opens with live processes in it.

    The job condition is not independent evidence of "nothing is running". It is
    the *consequence* of the supervisor being gone, because closing the last handle
    to a job destroys it -- measured on this host, a creator's job name fails to
    open with WinError 2 the moment that creator exits, and `KILL_ON_JOB_CLOSE`
    kills its members as it goes. So the job check confirms the supervisor is gone
    and the tree went with it; the supervisor and pid checks are what would catch
    a supervisor that is somehow still alive. All three are still asked, because
    each names a different way the evidence could be wrong, and this decision
    releases a resource two workers could otherwise share.

    Nothing is deleted and no claim is touched. The claim is released by
    `RELEASE_CLAIM`, which asks the guard again.
    """
    decided_at = datetime.now(timezone.utc).isoformat()
    with _write(store) as conn:
        row = conn.execute(
            "SELECT " + ", ".join(_RUN_COLUMNS) + " FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RecoveryRefused(
                f"refusing to abandon the launch of {run_id!r}: no run is recorded under that id"
            )
        record = dict(zip(_RUN_COLUMNS, row))
        if record["lifecycle"] == "terminal":
            raise RecoveryRefused(
                f"refusing to abandon the launch of {run_id!r}: it is already terminal with "
                f"result {record['result']!r}, and a finished run has nothing to abandon"
            )
        launch = conn.execute(
            "SELECT job_name, supervisor_pid, supervisor_start, abandoned FROM launches"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if launch is None:
            raise RecoveryRefused(
                f"refusing to abandon the launch of {run_id!r}: no launch was ever recorded "
                "for it, so there is no unresolved launch to decide"
            )
        job_name, supervisor_pid, supervisor_start, already = launch
        if already:
            raise RecoveryRefused(
                f"refusing to abandon the launch of {run_id!r}: it was already recorded as "
                "abandoned. Reconciliation is decided once, by a human, with the evidence "
                "they gave; a second request would be a second guess at it"
            )

        if _job_has_live_processes(job_name):
            raise RecoveryRefused(
                f"refusing to abandon the launch of {run_id!r}: its job {job_name!r} still "
                "opens and reports live processes, so the run's tree is still there"
            )
        if supervisor_pid:
            # Only asked once a supervisor has claimed the run. A launch whose
            # supervisor never started -- it died between the registration and
            # the claim, or the process that would have claimed it was killed --
            # has nobody to ask about, and the job condition above is then the only
            # evidence available. Refusing there would strand the run: the claim
            # could never be released, and there would be no evidence a human could
            # ever supply to change that.
            state = liveness(supervisor_pid, creation_time=supervisor_start)
            if state.state is not LivenessState.DEAD:
                raise RecoveryRefused(
                    f"refusing to abandon the launch of {run_id!r}: {state.detail}. A supervisor "
                    "that is still running owns a run whose outcome is not yet known"
                )
        process = json.loads(record["process_json"]) if record["process_json"] else None
        pid = (process or {}).get("pid")
        if process is not None:
            owner = _owned_process_liveness(process)
            if owner.state is not LivenessState.DEAD:
                raise RecoveryRefused(
                    f"refusing to abandon the launch of {run_id!r}: its recorded process at "
                    f"pid {pid} is {owner.state.value}, so the run's owner may still be writing"
                )

        conn.execute(
            "UPDATE launches SET abandoned = 1, abandoned_at = ?, abandon_evidence = ?"
            " WHERE run_id = ?",
            (decided_at, evidence, run_id),
        )
        if process is not None:
            reconciled = dict(process)
            reconciled[RECONCILIATION_KEY] = {
                "state": LAUNCH_ABANDONED,
                "evidence": evidence,
                "decided_at": decided_at,
                "exit_code": None,
            }
            conn.execute(
                "UPDATE runs SET process_json = ? WHERE run_id = ?",
                (json.dumps(reconciled, sort_keys=True), run_id),
            )


def _job_has_live_processes(job_name: str | None) -> bool:
    """Whether the recorded job still exists and still has members.

    A name that does not open is the expected answer for a run whose supervisor
    is gone, and it is a kill that has already happened rather than a tree that is
    still running. A name that does open is the opposite: something is still
    holding the job, and the run is not over.
    """
    if not job_name or sys.platform != "win32":
        return False
    try:
        import win32job
    except ImportError:
        return False
    try:
        handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, job_name)
    except Exception:
        return False
    try:
        accounting = win32job.QueryInformationJobObject(
            handle, win32job.JobObjectBasicAccountingInformation
        )
        return int(accounting.get("ActiveProcesses", 0)) > 0
    except Exception:
        # It opened, so something holds it, and this cannot prove otherwise.
        return True
    finally:
        with suppress(Exception):
            handle.Close()


def _release_claim(store: Store, key: str, evidence: str) -> None:
    claims = {claim["resource_key"]: claim for claim in _read_claims(store)}
    claim = claims.get(key)
    if claim is None:
        raise RecoveryRefused(
            f"refusing to release resource {key!r}: no claim is recorded under that key, "
            "so there is nothing to release"
        )

    generations = _read_generations(store)
    task_id = claim["task_id"]
    held_generation = claim["generation"]
    if task_id not in generations:
        reason = f"task {task_id!r} no longer exists"
    elif generations[task_id] == held_generation:
        raise RecoveryRefused(
            f"refusing to release resource {key!r}: task {task_id!r} is still at generation "
            f"{held_generation}, which is the generation holding it. The claim is current, "
            "not stale, and releasing it would take a live attempt's resource."
        )
    else:
        reason = (
            f"task {task_id!r} has advanced to generation {generations[task_id]} while the "
            f"resource is held at generation {held_generation}"
        )

    blocker = _live_holder(store, task_id, held_generation)
    if blocker is not None:
        raise RecoveryRefused(
            f"refusing to release resource {key!r}: {reason}, but {blocker}. A resource "
            "stays held while the attempt that owns it might still be writing."
        )

    with _write(store) as conn:
        # Both tables, in one transaction. Deleting only `claim_holders` would
        # leave the holder's membership row behind, and a capacity pool's `held`
        # is recomputed from those rows -- so the next release anywhere would set
        # the counter back up and hand a second task a slot that is in use. For an
        # exclusive resource it is worse: the row is gone, the primary key admits
        # a new claimant, and the resource is granted twice.
        cursor = conn.execute(
            "DELETE FROM claim_members"
            " WHERE resource_key = ? AND task_id = ? AND generation = ?",
            (key, task_id, held_generation),
        )
        if cursor.rowcount == 0:
            raise RecoveryRefused(
                f"refusing to release resource {key!r}: it changed hands while recovery was "
                "deciding, so the evidence named a claim that no longer exists"
            )
        # The counter and the row itself are claims' business, not recovery's, so
        # this delegates rather than restating the arithmetic a second time.
        from .claims import resync_claim_counts

        resync_claim_counts(conn)


def _live_holder(store: Store, task_id: str, generation: int | None = None) -> str | None:
    """Name a run that still might be writing, or None when none can be found.

    **This is the F13 guard, and the order of its questions is the fix.** It used
    to ask "does this run have a pid?" and skip the ones that did not. That single
    `continue` is the whole defect: a run registers, launches, and only publishes
    its identity after the check has finished, so for the entire run -- and forever
    after a crash -- the record said `preparing` with no pid. The guard read that
    as "nothing is running", found no holder, and released a claim that a live
    check was still writing to.

    So ownership is asked about first and liveness second. A run of unknown
    ownership is presumed live whatever its lifecycle says, because "I cannot prove
    what is running" and "nothing is running" are different states and only one of
    them is safe to release against.

    `generation` narrows the question to the runs of one generation, which is the
    claim actually being released. A run at a different generation does not block:
    the current generation's runs hold their own claims, so refusing on their
    behalf would protect nothing and would make the resource unreleasable while any
    later attempt existed.
    """
    for run in _read_runs(store):
        if run.task_id != task_id:
            continue
        if generation is not None and run.attempt != generation:
            continue
        if not run.unfinished and not (
            sys.platform != "win32"
            and (run.process or {}).get("ownership") == POSIX_OWNERSHIP
        ):
            continue
        if run.lifecycle in ("preparing", "launching") or not run.ownership_known:
            if run.abandoned:
                # The unresolved launch was decided by a human, on evidence, and
                # the decision is recorded. Refusing here again would make the
                # abandon unreachable: the only action that can clear this state
                # is the one this guard blocks, and a run nobody may ever release
                # is a claim nobody may ever hand on.
                continue
            return (
                f"run {run.run_id!r} is {run.lifecycle} and has published no verified "
                f"owner, so whether it is still writing cannot be ruled out. A missing "
                "identity is not evidence that nothing is running. If an operator has "
                "confirmed the launch is over, apply ABANDON_LAUNCH to this run with "
                "that evidence first"
            )
        state = _owned_process_liveness(run.process)
        if state.state is LivenessState.ALIVE:
            return f"run {run.run_id!r} is {run.lifecycle} and its process is alive ({state.detail})"
        if state.state is LivenessState.UNCERTAIN:
            return (
                f"run {run.run_id!r} is {run.lifecycle} and its liveness is uncertain "
                f"({state.detail}), which is treated as alive"
            )
    return None


def _mark_run_dead(store: Store, run_id: str, evidence: str) -> None:
    """Record that a run's process is confirmed gone, so it can be reconciled.

    The liveness check happens inside the write transaction. Checking outside it
    would leave a window where the decision was made against one state of the
    record and written against another.
    """
    decided_at = datetime.now(timezone.utc).isoformat()
    with _write(store) as conn:
        row = conn.execute(
            "SELECT " + ", ".join(_RUN_COLUMNS) + " FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: no run is recorded under that id"
            )
        record = dict(zip(_RUN_COLUMNS, row))
        process = json.loads(record["process_json"]) if record["process_json"] else None
        pid = process.get("pid") if process else None

        if record["lifecycle"] == "terminal":
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: it is already terminal with result "
                f"{record['result']!r}, and a terminal run is not reconciled by liveness"
            )
        if record["lifecycle"] in ("preparing", "launching", "cancelling"):
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: it is {record['lifecycle']} and its "
                "ownership was never established, so there is no pid whose death could be "
                "confirmed. ABANDON_LAUNCH is the action for this state, because it carries "
                "the job-name evidence that a dead-pid check does not have"
            )
        if _reconciled(process):
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: it is already recorded as "
                f"{DEAD_CONFIRMED}. Reconciliation is decided once, by a human, with the "
                "evidence they gave; a second request would be a second guess at it"
            )
        if not isinstance(pid, int):
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: it records no process identity, so "
                "there is no pid whose death could be confirmed"
            )

        state = _owned_process_liveness(process)
        if state.state is LivenessState.ALIVE:
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: {state.detail}. A running process is "
                "never abandoned on the strength of an old timestamp"
            )
        if state.state is LivenessState.UNCERTAIN:
            raise RecoveryRefused(
                f"refusing to mark run {run_id!r} dead: {state.detail}. An uncertain process "
                "is preserved and its claims stay held"
            )

        reconciled = dict(process)
        reconciled[RECONCILIATION_KEY] = {
            "state": DEAD_CONFIRMED,
            "evidence": evidence,
            "decided_at": decided_at,
            "exit_code": state.exit_code,
        }
        conn.execute(
            "UPDATE runs SET process_json = ? WHERE run_id = ?",
            (json.dumps(reconciled, sort_keys=True), run_id),
        )


__all__ = [
    "Action",
    "Finding",
    "FindingKind",
    "Liveness",
    "LivenessState",
    "RecoveryRefused",
    "Report",
    "apply_action",
    "inspect",
    "liveness",
]
