"""Start a run that outlives the process that asked for it, and cancel it safely.

Plan 01 executes a check in the foreground. This module adds the two things
that need more than a function call: a launch that survives the initiating
client exiting, and a cancellation that cannot kill a stranger.

**The architecture is measured, not assumed.** A Windows Job Object handle lives
in the process that created it, so an anonymous job is unreachable from a second
terminal. Naming the job does not help on its own either: `JOB_OBJECT_LIMIT_KILL_
ON_JOB_CLOSE` destroys the tree the moment the last handle closes, and a named
job does not survive its creator -- measured on this host, `OpenJobObject` on a
creator's job name fails with WinError 2 once that creator has exited, even while
a member process is still running. So the launcher mints the name and records it
durably *before* creating anything, and the supervisor creates the job from that
recorded name and holds the handle open for the whole run. A cancel from any other
process finds the job by name and terminates the tree.

**What this module no longer does.** It does not execute the check, and it does
not decide whether a run can be cancelled. `start_run` records a launch and
returns; `vkit.supervise` is the only writer of a run's process identity after the
process is created. The client no longer supplies an owner pid, because the
record already holds one and a client-supplied identity is a client-supplied
identity.

The security consequence is recorded rather than assumed: anyone who can open a
named job can terminate the run, so the name carries a random token and is not
derivable from the run id.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .execution import RunEnvironment
from .identity import compute_source_identity
from .manifest import Manifest, parse_manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed, ScenarioResult
from .paths import Project
from .procs import JobLease, job_name_for, prepare_job
from .procidentity import still_the_same_process
from .storage import REPORT_NAME, LaunchPlan, Store


class SupervisorError(Exception):
    """The supervisor could not start or confirm a run."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunHandoff:
    """What a caller gets back from starting a run, before it has an outcome.

    There is no `outcome` and no `report`, and their absence is the point rather
    than an omission. A detached supervisor returns before the check has run, so
    the only truthful thing to hand back is the run's identity, the attempt it
    belongs to, and the lifecycle recorded so far. A caller that wanted the
    verdict asks for it afterwards, and `replayed` is how it learns that this
    call did not start anything.
    """

    run_id: str
    check_id: str
    task_id: str | None
    generation: int | None
    lifecycle: str
    started_at: str
    replayed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "check_id": self.check_id,
            "task_id": self.task_id,
            "generation": self.generation,
            "lifecycle": self.lifecycle,
            "started_at": self.started_at,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class StartedRun:
    """A registered run and the outcome it reached.

    Retained for the inline path, where the check has finished by the time this
    is returned, and for the two callers that execute in-process. It is not what
    a detached start returns: a caller that gets this has an outcome, and a
    caller that does not gets a `RunHandoff`.
    """

    run_id: str
    outcome: Outcome
    report: dict[str, Any]

    @property
    def launched(self) -> bool:
        return bool(self.report.get("process"))


def outcome_from_report(report: dict[str, Any]) -> Outcome:
    """Rebuild the Outcome from a stored report.

    A retry must return the verdict that was already recorded, not re-derive it
    and certainly not re-run the check. This is the one place that translation
    lives, so a stored FAIL cannot be read back as a PASS.
    """
    body = report.get("outcome") or {}
    result = body.get("result")
    if result in ("PASS", "FAIL"):
        scenarios = tuple(
            ScenarioResult(s["id"], s["result"] == "PASS", s["observation"])
            for s in body.get("scenarios", ())
        )
        return Passed(scenarios) if result == "PASS" else Failed(scenarios)
    try:
        reason = BlockedReason(body.get("reason", "internal_error"))
    except ValueError:
        reason = BlockedReason.INTERNAL_ERROR
    return Blocked(reason, body.get("detail", ""))


def start_run(
    project: Project,
    store: Store,
    check_id: str,
    *,
    task_id: str | None = None,
    generation: int | None = None,
    manifest: Manifest | None = None,
    run_id: str | None = None,
    detach: bool = True,
    env: RunEnvironment | None = None,
) -> RunHandoff:
    """Record a launch and return, without waiting for the check to finish.

    The ordering is the whole design. The job is created and named first, then
    the run row is registered, then the launch is recorded, and only then is a
    supervisor started. Each of those commits before the next begins, so at every
    instant after a run id exists the durable record can answer what was launched
    and who owns it. A crash at any point leaves a state recovery can name rather
    than a state it has to guess at.

    `detach=True` starts a supervisor process and returns at once. `detach=False`
    runs the same body in this process, which is what `vkit check run` and the
    console want: they hold an open connection and expect the verdict when the
    call returns. The body is the same either way, so the two cannot drift.

    A `run_id` that already has a recorded launch is a replay. It returns the
    recorded lifecycle and does not launch a second time, whatever that lifecycle
    is. The earlier version refused only a registered run with no report, which
    flattened three different states into one refusal and locked out permanently
    the one run that most needed a remedy.
    """
    manifest = manifest or parse_manifest(project, project.runs_root / "probe")
    check = manifest.require(check_id)
    run_id = run_id or uuid.uuid4().hex

    existing = store.load_launch(run_id)
    if existing is not None:
        return _handoff(store, run_id, check.id, task_id, generation, replayed=True)

    if task_id is not None:
        # The caller must still own everything the task requires, and it must do
        # so before a row or a job exists. Checked here rather than left to the
        # supervisor, because the supervisor is a separate process by the time it
        # could check, and a resource released in between would be a run writing
        # to a checkout this attempt no longer holds.
        from .tasks import ConflictError as _TaskConflict, TaskError, verify_ownership

        try:
            verify_ownership(store, task_id, generation if generation is not None else 0)
        except (_TaskConflict, TaskError) as exc:
            raise SupervisorError(f"refusing to start run {run_id}: {exc}") from exc

    environment = env or RunEnvironment()
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    argv = tuple(str(part) for part in check.resolved_argv_for(run_dir, environment.python))
    # Recorded absolutely so the detached supervisor re-parses the same manifest
    # this run was decided against, rather than resolving one from whatever
    # working directory it happens to inherit.
    manifest_dir = project.manifest_path.parent

    # The name is minted and recorded before the job is created, because for a
    # detached run the job is created by the supervisor rather than by this
    # process. It cannot be created here: this process has to release its handle
    # before the supervisor starts, and closing the last handle of a job is the
    # kill. A named job does not survive its creator either -- measured,
    # `OpenJobObject` on a creator's job name fails with WinError 2 once that
    # creator has exited -- so the supervisor creates the job from this name.
    job_name = job_name_for(run_id)
    lease: JobLease | None = None
    try:
        source = compute_source_identity(project)
        store.register_run(
            run_id, check.id, task_id=task_id, attempt=generation,
            source=source.to_json(), configuration_digest=manifest.digest(),
            fixture_digest=None,
        )
        plan = LaunchPlan(
            project_root=project.root,
            state_dir=project.state_root,
            manifest_dir=manifest_dir,
            check_id=check.id,
            argv=argv,
            cwd=Path(check.cwd),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=environment.provenance(),
            timeout_seconds=float(check.timeout_seconds),
            job_name=job_name,
            kind="detached" if detach else "inline",
        )
        store.record_launch(run_id=run_id, task_id=task_id, generation=generation, plan=plan)
    except BaseException:
        raise

    if detach:
        _spawn_supervisor(project, run_id)
        return _handoff(store, run_id, check.id, task_id, generation, replayed=False)

    # Inline: this process is the supervisor, so it holds the job for the whole
    # run. Created after the registration so that a failure here leaves a run that
    # is registered and launched rather than a job nothing will ever own.
    try:
        lease = prepare_job(run_id, job_name)
        from .supervise import supervise_run

        supervise_run(store, run_id, lease=lease, env=environment)
    finally:
        if lease is not None:
            lease.close()
    return _handoff(store, run_id, check.id, task_id, generation, replayed=False)


def _handoff(
    store: Store, run_id: str, check_id: str, task_id: str | None,
    generation: int | None, *, replayed: bool,
) -> RunHandoff:
    """Read the recorded lifecycle back, so the handoff states what is true now."""
    status = store.run_status(run_id) or {}
    return RunHandoff(
        run_id=run_id,
        check_id=status.get("check_id") or check_id,
        task_id=status.get("task_id", task_id),
        generation=status.get("attempt", generation),
        lifecycle=status.get("lifecycle", "preparing"),
        started_at=status.get("registered_at") or _now(),
        replayed=replayed,
    )


def _spawn_supervisor(project: Project, run_id: str) -> subprocess.Popen:
    """Start the detached supervisor for this run.

    The state directory is passed as an argument rather than resolved from the
    working directory. The supervisor has to re-open exactly the store this
    launcher wrote, and a supervisor that resolved the project from its own cwd
    would report "nothing to supervise" -- which is indistinguishable from a run
    that has not been asked for yet, and leaves the row `preparing` forever.

    `PYTHONPATH` names *this interpreter's own* vkit package directory, taken from
    the module that is running rather than from the working directory or the
    manifest. An editable install puts one absolute `src` on `sys.path` through a
    `.pth` file, so a child that inherits the environment resolves the installed
    copy rather than the one that launched it. Measured on this host: with
    `PYTHONPATH=src` set only in the parent, `python -m vkit.supervise` failed with
    "No module named vkit.supervise" while `import vkit` in the parent resolved
    fine. A supervisor that silently imported a different vkit would be the worst
    possible failure here, so its source is pinned to the running package's own
    directory.

    `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` so the supervisor survives the
    client exiting and is not in the client's console group, which is what would
    otherwise deliver a Ctrl-C to it at the same moment as to the client. Its
    stdout and stderr go to the run directory, so a supervisor that fails to start
    leaves a trace where the run's other evidence is rather than on a handle the
    client no longer holds.

    Started through `subprocess` rather than `win32process` because no standard
    handle has to be inherited: the supervisor talks to the database, not to a
    pipe this process would then have to pump.
    """
    run_dir = project.runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "vkit.supervise", run_id, str(project.state_root)]
    package_root = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        package_root if not existing else os.pathsep.join([package_root, existing])
    )
    log = (run_dir / "supervisor.log").open("ab")
    try:
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(
                argv, cwd=str(project.root), stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True, env=env,
            )
        return subprocess.Popen(
            argv, cwd=str(project.root), stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env,
        )
    finally:
        log.close()


def cancel_run(
    store: Store,
    run_id: str,
    *,
    requested_by: str = "cli",
) -> tuple[Outcome, dict[str, Any]]:
    """Cancel a run by the identity the run itself recorded.

    The client supplies nothing but the run id. It used to supply the owner pid
    and creation time as well, and the core then checked them against the record,
    which is a second authority for a fact the record already holds: a client that
    named a different pid was told it was wrong, and a client that named the right
    one had learned nothing it could not have read. A client-supplied identity is
    a client-supplied identity, so the parameter is gone rather than validated.

    The order is fixed and each step fails differently. A run that already
    finished returns the outcome it actually reached. A run with no known owner
    records a cancel intent and reports it as pending, because "nothing to cancel
    yet" is not a settled verdict on the run. A run whose owner is verified is
    terminated through the job recorded at launch. A failure at any of those
    leaves the run BLOCKED with `ownership_lost` and the claims held, because
    freeing a resource whose owner may still be alive is how two workers end up
    writing the same checkout.
    """
    report_path = store.run_dir(run_id) / REPORT_NAME
    if report_path.is_file():
        stored = json.loads(report_path.read_text(encoding="utf-8"))
        if stored.get("lifecycle") == "terminal":
            # Already finished. Cancelling a finished run returns the outcome it
            # actually reached rather than inventing a cancellation.
            return outcome_from_report(stored), stored

    recorded = store.run_process_identity(run_id) or {}
    launch = store.load_launch(run_id)
    if not recorded.get("pid") or not recorded.get("ownership_known"):
        # The cancel arrived before the run had an owner it could be aimed at. It
        # is recorded, not applied, and the supervisor picks it up before it
        # begins executing. Publishing BLOCKED/ownership_lost here would answer
        # a question about the run that the run has not finished being asked.
        intent = store.record_cancel_intent(run_id, requested_by)
        return Blocked(BlockedReason.CANCELLED, _pending_detail(run_id, intent)), {
            "run_id": run_id,
            "lifecycle": "cancelling",
            "outcome": Blocked(BlockedReason.CANCELLED, _pending_detail(run_id, intent)).to_json(),
            "cancelled": False,
            "pending": True,
            "cancel_intent": intent,
        }

    identity = _recorded_identity(run_id, recorded)
    if not still_the_same_process(identity):
        return _blocked_publish(
            store, run_id, recorded, launch,
            BlockedReason.OWNERSHIP_LOST,
            f"pid {identity.pid} is not the process this run launched, or no longer "
            "exists; refusing to signal it, because the pid may have been recycled",
        )

    # The name is read from the launch record, not from `process_json`, and a
    # launch record is the earlier and the more durable of the two: the name is
    # written before the process exists. A reader that only consulted
    # `process_json` would find no job name for exactly the runs whose ownership
    # is least certain.
    job_name = (launch or {}).get("job_name") or recorded.get("job_name")
    try:
        terminate_owned_tree(job_name)
    except OSError as exc:
        return _blocked_publish(
            store, run_id, recorded, launch,
            BlockedReason.OWNERSHIP_LOST,
            f"could not terminate the owned tree: {exc}; the claim is retained",
        )

    store.resolve_cancel_intent(run_id)
    blocked = Blocked(
        BlockedReason.CANCELLED,
        f"cancelled on request; the owned tree for pid {identity.pid} was stopped",
    )
    report = _cancel_report(run_id, recorded, blocked, identity, launch)
    store.publish(run_id, report)
    # `cancelled` and `pending` are this function's own answers and are added only
    # to the returned view. The published document is the evidence, and
    # `additionalProperties: false` means a convenience key written into it would
    # make the report fail the schema it is validated against.
    return blocked, {**report, "cancelled": True, "pending": False}


def _pending_detail(run_id: str, intent: dict[str, Any]) -> str:
    return (
        f"run {run_id} has no published owner yet, so the cancel is recorded and "
        "pending rather than applied. It will be honoured before the check begins "
        f"executing. Requested by {intent.get('requested_by', 'an unknown caller')}."
    )


def _recorded_identity(run_id: str, recorded: dict[str, Any]):
    """The identity the run published, rebuilt for verification.

    `still_the_same_process` is what decides whether a signal is safe, and it
    needs the whole recorded pair. A record carrying a pid but no creation time
    cannot prove ownership of anything -- a bare pid fails every comparison -- so
    that is refused here with the actual defect named, rather than reaching the
    comparison as a value that never matches and reporting a live process as a
    stranger.
    """
    from .procidentity import ProcessIdentity

    pid = recorded.get("pid")
    creation_time = recorded.get("creation_time")
    if not isinstance(creation_time, int) or isinstance(creation_time, bool) or creation_time <= 0:
        raise SupervisorError(
            f"run {run_id} recorded pid {pid} without the creation time that identifies "
            "it, so ownership cannot be proven. Cancelling on a bare pid is refused "
            "because pids are recycled; reconcile this run instead"
        )
    boot_id = recorded.get("boot_id")
    return ProcessIdentity(
        pid=int(pid), creation_time=int(creation_time),
        boot_id=boot_id if isinstance(boot_id, str) else "",
    )


def terminate_owned_tree(job_name: str | None) -> None:
    """Terminate the container owning a run's tree, from any process.

    On Windows that is a job object, keyed to a handle, and this has been
    verified: the object is created before the child is resumed and
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes closing the last handle a kill.

    On POSIX the unit is a process group, and the group is what gets signalled.
    That the signal reaches a whole tree is now measured rather than assumed:
    scripts/measure_posix_group.py and tests/test_procs_posix_real.py both
    observe the child, the grandchild and the great-grandchild dead after one
    signal, with no survivor anywhere in the group.

    There is a second, independent reason, and it is the one that decides this.
    A group signal is a broadcast to the pids in a group, and a descendant that
    calls setsid is not in that group. Measured in
    scripts/measure_posix_escape.py, in the same run and under the same signal
    that killed the ordinary grandchild: the leader and its ordinary grandchild
    died within 20ms with no running process left in the group, and the setsid
    descendant was still running. So even a cancel that happened while the
    leader was still alive would miss a descendant that left the group, which is
    a smaller failure than the one below and is not the reason this refuses.

    What is NOT provided, and is the reason this still refuses, is the property
    the job object gives for free. A Windows tree cannot outlive the handle that
    owns it. A POSIX process group is a set of pids the kernel keeps after its
    leader is gone, so a group outlives the process that created it. Measured in
    scripts/measure_supervisor_death.py: a descendant in its own session was
    still running after its owner was SIGKILLed. There is no POSIX equivalent of
    a kill-on-last-handle-close, so a cancel that must not leave a tree behind
    cannot be built from a process group.

    A job object closes both gaps at once, which is why the Windows path is a job
    object and not a walk of the parent-child tree. It is held by a HANDLE rather
    than by group membership, so a descendant that escapes a group is still
    inside the job, and the last handle closing is a kill whether or not the
    process that owned it is still running.

    So the refusal stands, and it names the real reason: not "unverified", which
    was true when this was written and is not now, but "no mechanism exists".
    """
    if sys.platform == "win32":
        if not job_name:
            raise OSError("no job name was recorded for this run")
        import win32job

        handle = win32job.OpenJobObject(win32job.JOB_OBJECT_TERMINATE, False, job_name)
        try:
            win32job.TerminateJobObject(handle, 1)
        finally:
            handle.Close()
        return

    raise OSError(
        "POSIX cancellation cannot guarantee the tree is stopped. A process group "
        "is signalled on timeout and the signal is measured to reach every "
        "descendant that stays in it, but a group is not keyed to its creator: the "
        "kernel keeps it after the leader exits, and POSIX has no equivalent of a "
        "job object's kill-on-last-handle-close. A descendant that calls setsid "
        "also leaves the group and cannot be signalled at all. A run cancelled "
        "from another process may therefore leave descendants behind, so the "
        "claim is retained and the operator reconciles it."
    )


def _blocked_publish(
    store: Store, run_id: str, recorded: dict[str, Any], launch: dict[str, Any] | None,
    reason: BlockedReason, detail: str,
) -> tuple[Outcome, dict[str, Any]]:
    blocked = Blocked(reason, detail)
    report = _cancel_report(run_id, recorded, blocked, None, launch)
    store.publish(run_id, report)
    return blocked, report


def _cancel_report(
    run_id: str, recorded: dict[str, Any], blocked: Blocked, identity: Any,
    launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A terminal report for a cancelled or un-cancellable run.

    It records what the store knows rather than inventing provenance: a run that
    never launched has no source identity or command to report, and pretending
    otherwise would put a fabrication in the durable evidence. The argv comes
    from the `launches` row when there is one, because that row is written before
    the process exists and is therefore the only place the command is known for a
    run that was cancelled before it could report it itself.

    `ownership_known` travels on the report's `process` object, and only when it
    is false. A cancel report for a run whose owner was never published is exactly
    the case where a reader needs to be told that absence is not absence of a
    process, and a report that omitted the key would read as a run with no
    process at all.
    """
    recorded_launch = launch or {}
    process: dict[str, Any] | None = None
    if identity is not None:
        process = {
            "pid": identity.pid,
            "creation_time": identity.creation_time,
            "ownership": recorded.get("ownership") or "windows_job_object",
            "exit_code": None,
            "timed_out": False,
        }
        if recorded.get("boot_id"):
            process["boot_id"] = recorded["boot_id"]
        job_name = recorded.get("job_name") or recorded_launch.get("job_name")
        if job_name:
            process["job_name"] = job_name
    else:
        # No verified owner. The key is what stops this being read as a run that
        # never started, which is the inference the whole milestone exists to
        # prevent.
        process = {
            "pid": 0,
            "ownership": recorded.get("ownership") or "windows_job_object",
            "exit_code": None,
            "timed_out": False,
            "ownership_known": False,
        }
    argv = recorded.get("command", {}).get("argv") if isinstance(recorded.get("command"), dict) else None
    if not argv:
        argv = recorded_launch.get("argv_json")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "check_id": recorded.get("check_id") or recorded_launch.get("check_id") or "unknown",
        "lifecycle": "terminal",
        "started_at": recorded.get("registered_at") or recorded_launch.get("requested_at") or _now(),
        "ended_at": _now(),
        "outcome": blocked.to_json(),
        "source": recorded.get("source") or {"head": "unknown", "inventory_digest": "unknown", "dirty": False},
        "command": recorded.get("command") or {
            "argv": json.loads(argv) if isinstance(argv, str) and argv else ["<never launched>"],
            "cwd": recorded_launch.get("cwd") or "<none>",
        },
        "configuration_digest": recorded.get("configuration_digest") or "unknown",
        "fixture_digest": None,
        "process": process,
        "artifacts": {},
        "environment": {},
        "logs": {"stdout": None, "stderr": None},
    }
