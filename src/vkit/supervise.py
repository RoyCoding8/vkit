"""The process that owns one run from launch to published outcome.

Started as `python -m vkit.supervise <run_id>`. It re-opens the store from the
absolute path the launcher recorded, creates the job from the name that was
recorded before anything was created, launches the check, publishes the run's
identity as soon as it exists, waits, and publishes the terminal report.

**Why this is a module and not a function inside `supervisor`.** Two reasons,
and the second is the one that bites. The first is that the job handle has to be
held open across the client's disconnect, and a handle cannot be held by a process
that has already returned. The second is that `python -m` only executes a module
through a `__main__` guard. A supervisor module with a single function and no
guard starts, exits 0, and has done nothing: `start_run` would return a handoff
for a run that will never launch, the driver would never be spawned, and the row
would sit `preparing` forever. That is the exact window this milestone exists to
close, so the guard is not optional and not an afterthought.

**The ordering here is the durable record.** Each write commits before the next
step begins:

1. read the `launches` row, which is the authority on what to run
2. create the job from the recorded name
3. `launch`, which creates the child suspended, contains it, reads its creation
   FILETIME and resumes it
4. `mark_launching`, so a crash between here and step 6 is still a recorded
   unresolved launch rather than a silent gap
5. check for a cancel that arrived before now, and honour it
6. `publish_identity`, so the pid and the ownership become durable
7. `await_exit`, the wait and the timeout
8. derive the outcome and publish the terminal report

Steps 4 and 6 are two writes rather than one because the window between them is
the dangerous one, and a reader that could not tell "a process was created" from
"a process was created and here is which one" would be back to guessing. The
fault-injection seam below sits exactly between them, which is the only place a
crash is worth reproducing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .execution import RunEnvironment
from .manifest import Manifest, parse_manifest
from .outcome import Blocked, BlockedReason
from .paths import DB_NAME, Project
from .procs import JobLease, await_exit, launch
from .storage import Store, StoreError


def _fault(point: str) -> None:
    """Exit hard at a named point, for a test that needs a crash a `finally` cannot survive.

    `os._exit` rather than an exception, because the point of the test is a
    process that stops existing: no unwinding, no cleanup, no `finally`. Handles
    the OS closes on the way out still close, so `KILL_ON_JOB_CLOSE` still fires
    and the tree is still gone -- which is the guarantee the other half of this
    test is checking, and it is checked rather than assumed.

    One private seam, read only here, named by the test that needs it.
    """
    if os.environ.get("VKIT_FAULT") == point:
        sys.stderr.write(f"VKIT_FAULT {point}: exiting without publishing\n")
        sys.stderr.flush()
        os._exit(9)


def _cancel_if_requested(store: Store, run_id: str) -> bool:
    """Whether a cancel arrived before the check began executing.

    Checked here and again after the identity is published. A cancel that lands
    during startup is a deferred cancel, never a lost one and never a
    cancelled-then-running run, and the only ordering that guarantees that is
    this one: the supervisor looks before it starts work, and before it waits.
    """
    return store.cancel_intent(run_id) is not None


def _environment_from_launch(launch_row: dict[str, Any]) -> RunEnvironment:
    """Rebuild the execution environment the launcher decided on.

    Read from the launch record rather than inherited, because the supervisor is a
    separate process with its own environment and a run's provenance has to be the
    one the launcher recorded. An ambient launch records `{"mode": "ambient"}`,
    which is the default and is what a development run means.
    """
    import json

    recorded = launch_row.get("env_json") or "{}"
    try:
        provenance = json.loads(recorded)
    except ValueError:
        return RunEnvironment()
    if not isinstance(provenance, dict) or provenance.get("mode") != "trusted_integration":
        return RunEnvironment()
    return RunEnvironment(
        python=provenance.get("python") if provenance.get("python") not in (None, "inherited") else None,
        plugin_root=Path(provenance["plugin_root"]) if provenance.get("plugin_root") else None,
        revalidate=provenance.get("artifact_revalidated") == "yes",
        verifier_revision=provenance.get("verifier_revision"),
    )


def _publish_pending_cancel(store: Store, run_id: str, launch_row: dict[str, Any]) -> None:
    """Publish the terminal outcome for a cancel that arrived before execution.

    Nothing was launched, so there is no tree to stop and no identity to verify.
    The report says the run was cancelled and carries `ownership_known: false`,
    so a reader is not left inferring from an absent process that nothing ever
    started.
    """
    from .supervisor import _cancel_report

    blocked = Blocked(
        BlockedReason.CANCELLED,
        f"cancelled before the check began executing; nothing was launched for run {run_id}",
    )
    report = _cancel_report(run_id, {}, blocked, None, launch_row)
    store.resolve_cancel_intent(run_id)
    try:
        store.publish(run_id, report)
    except StoreError:
        # Already terminal, which means something else finished the run first and
        # its outcome is the one that stands.
        pass


def supervise_run(
    store: Store, run_id: str, *, lease: JobLease | None = None, env: RunEnvironment | None = None
) -> None:
    """Own one run from its recorded launch to its published outcome.

    `lease` is the already-created job, for the inline path where this process
    created it. The detached path passes None and the job is created here from
    the recorded name, because the process that minted the name has to release
    its handle before this one starts and closing the last handle is the kill.
    """
    import json

    launch_row = store.load_launch(run_id)
    if launch_row is None:
        raise StoreError(f"run {run_id} has no recorded launch to supervise")

    project = _project_from_launch(launch_row)
    manifest = _manifest_from_launch(project, launch_row)
    check = manifest.require(launch_row["check_id"])
    environment = env or _environment_from_launch(launch_row)

    status = store.run_status(run_id)
    if status is None:
        raise StoreError(f"run {run_id} is not registered")
    if status["lifecycle"] == "terminal":
        return

    if _cancel_if_requested(store, run_id):
        _publish_pending_cancel(store, run_id, launch_row)
        return

    held = lease
    created_here = False
    if held is None:
        held = _create_job(launch_row)
        created_here = True

    published_identity = False
    try:
        running = launch(
            json.loads(launch_row["argv_json"]),
            cwd=Path(launch_row["cwd"]),
            stdout_path=Path(launch_row["stdout_path"]),
            stderr_path=Path(launch_row["stderr_path"]),
            timeout_seconds=float(launch_row["timeout_seconds"]),
            job=held,
        )
        # A launch that produced no pid is a real answer and not a crash: the
        # command could not be started. `run_check` below reports it as
        # BLOCKED/launch_failed, and the identity write below is what records
        # that nothing was created.
        if running.pid is None:
            store.mark_launching(run_id, {
                "check_id": launch_row["check_id"],
                "command": {
                    "argv": json.loads(launch_row["argv_json"]),
                    "cwd": launch_row["cwd"],
                },
                "launch_failed": running.detail_reason and str(running.detail_reason),
            })
            published_identity = True
        else:
            store.mark_launching(run_id, {
                "pid": running.pid,
                "creation_time": running.creation_time,
                **({"boot_id": running.boot_id} if running.boot_id else {}),
                "ownership": running.ownership,
                "job_name": launch_row["job_name"],
                "check_id": launch_row["check_id"],
                "command": {
                    "argv": json.loads(launch_row["argv_json"]),
                    "cwd": launch_row["cwd"],
                },
            })
            _fault("after_launching")
            # Checked again here, so a cancel that arrived while the process was
            # being created is honoured before the wait rather than after it.
            if _cancel_if_requested(store, run_id):
                await_exit(running)
                _publish_cancelled(store, run_id, launch_row, running)
                published_identity = True
            else:
                store.publish_identity(run_id, running.identity())
                published_identity = True
                result = await_exit(running)
                _publish_outcome(store, run_id, launch_row, running, result, manifest, check, environment)
                return
        if not published_identity:
            store.publish_identity(run_id, running.identity())
    finally:
        if created_here and held is not None:
            held.close()


def _create_job(launch_row: dict[str, Any]) -> JobLease | None:
    from .procs import prepare_job

    name = launch_row.get("job_name")
    if not name:
        return None
    return prepare_job(launch_row["run_id"], name)


def _publish_cancelled(
    store: Store, run_id: str, launch_row: dict[str, Any], running: JobLease
) -> None:
    from .supervisor import _cancel_report

    recorded = {
        "pid": running.pid,
        "creation_time": running.creation_time,
        "ownership": running.ownership,
        "check_id": launch_row["check_id"],
        "registered_at": launch_row["requested_at"],
    }
    blocked = Blocked(
        BlockedReason.CANCELLED,
        f"cancelled on request; the owned tree for pid {running.pid} was stopped",
    )
    identity = running.identity()
    report = _cancel_report(run_id, recorded, blocked, identity, launch_row)
    store.resolve_cancel_intent(run_id)
    try:
        store.publish(run_id, report)
    except StoreError:
        pass


def _publish_outcome(
    store: Store, run_id: str, launch_row: dict[str, Any], running: JobLease,
    result: Any, manifest: Manifest, check: Any, environment: RunEnvironment,
) -> None:
    """Turn the finished process into the run's one terminal outcome.

    The run row is already registered and already owns its identity, so the
    report is built here rather than by `run_check`, which would register a
    second run under this id. That is what keeps the report, the identity and the
    launch record describing the same execution rather than two.
    """
    import json as _json
    from datetime import datetime, timezone

    from .execution import ProcessResult, _derive, _publish, _terminal_report

    process = ProcessResult(
        pid=result.pid,
        ownership=result.ownership,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        launch_error=None if result.reason is None else (result.reason, result.detail),
        creation_time=result.creation_time,
        boot_id=result.boot_id,
    )
    artifact = store.resolve_artifact(run_id, check.artifact_name)
    outcome = _derive(check, process, artifact if artifact.is_file() else None)
    report = _terminal_report(
        run_id, check, manifest, _source_of(store, run_id),
        outcome=outcome,
        started_at=launch_row["requested_at"],
        ended_at=datetime.now(timezone.utc).isoformat(),
        process=process,
        artifact=artifact,
        run_dir=store.run_dir(run_id),
        executed_argv=list(_json.loads(launch_row["argv_json"])),
        provenance=environment.provenance(),
    )
    _publish(store, run_id, report)


def _source_of(store: Store, run_id: str):
    """The source identity the launcher recorded, rebuilt for the report.

    Recomputing it here would be wrong: the report has to describe the tree the
    check ran against, and the tree may have moved since the launch was recorded.
    The recorded identity is what the run was admitted against.

    Built field by field against `SourceIdentity` rather than through a parser,
    because the dataclass has no `from_json` and inventing one here would be a
    second definition of how a stored identity becomes a value.
    """
    import json

    from .identity import SourceIdentity

    with store._connect() as conn:
        raw = conn.execute(
            "SELECT source_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    document = json.loads(raw[0]) if raw and raw[0] else {}
    return SourceIdentity(
        head=document.get("head", "unknown"),
        inventory_digest=document.get("inventory_digest", "unknown"),
        dirty=bool(document.get("dirty", False)),
        tracked_files=int(document.get("tracked_files", 0)),
        dirty_paths=tuple(document.get("dirty_paths", ())),
    )


def _project_from_launch(launch_row: dict[str, Any]) -> Project:
    from .paths import open_project

    return open_project(launch_row["project_root"])


def _manifest_from_launch(project: Project, launch_row: dict[str, Any]) -> Manifest:
    return parse_manifest(project, Path(launch_row["manifest_dir"]))


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m vkit.supervise <run_id>`.

    The state directory is passed rather than resolved. A detached supervisor is
    started with a run id and the absolute path to the store it must re-open, and
    it opens exactly that. Resolving the project from the working directory
    instead would mean a supervisor that silently reports "nothing to supervise"
    if it were started anywhere else, and a run that never launches looks exactly
    like a run that has not been asked for yet.

    Returns 0 on success and 1 on a failure that is not otherwise reported, so a
    launcher that waits on this process can tell the two apart. The run's own
    outcome is published through the store, not through this exit code: a
    supervisor that fails before it can publish leaves the row for recovery to
    name, which is the honest outcome.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        sys.stderr.write("usage: python -m vkit.supervise <run_id> <state_dir>\n")
        return 2
    run_id, state_dir = args

    store = Store(Path(state_dir) / DB_NAME)
    launch_row = store.load_launch(run_id)
    if launch_row is None:
        sys.stderr.write(f"run {run_id} has no recorded launch; nothing to supervise\n")
        return 1
    lease = _create_job(launch_row)
    try:
        supervise_run(store, run_id, lease=lease)
    except StoreError as exc:
        sys.stderr.write(f"run {run_id} could not be supervised: {exc}\n")
        return 1
    finally:
        if lease is not None:
            # The last handle to the job closing. For a run that has already
            # published its outcome there is nothing left in the job, so this is
            # not a kill of anything; for a run that failed mid-flight it is
            # exactly the containment the job exists to provide.
            lease.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
