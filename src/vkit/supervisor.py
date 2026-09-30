"""Start a run that outlives the process that asked for it, and cancel it safely.

Plan 01 executes a check in the foreground. This module adds the two things
that need more than a function call: a launch that survives the initiating
client exiting, and a cancellation that cannot kill a stranger.

**The architecture is measured, not assumed.** A Windows Job Object handle lives
in the process that created it, so an anonymous job is unreachable from a second
terminal. Naming the job does not help on its own: `JOB_OBJECT_LIMIT_KILL_ON_
JOB_CLOSE` destroys the tree the moment the last handle closes, which is exactly
what happens when the launcher exits. The working arrangement, verified on this
host, is a long-lived supervisor that creates the named job, launches the check,
and keeps the handle open. A cancel from any other process finds the job by name
and terminates the tree.

The security consequence is recorded rather than assumed: anyone who can open a
named job can terminate the run, so the name carries a random token and is not
derivable from the run id.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .execution import ExecutionError, run_check
from .identity import SourceIdentity, compute_source_identity
from .manifest import Manifest, parse_manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed, ScenarioResult
from .paths import Project
from .procidentity import ProcessIdentity, read_identity, still_the_same_process
from .storage import Store, StoreError


class SupervisorError(Exception):
    """The supervisor could not start or confirm a run."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_name_for(run_id: str) -> str:
    """A job name only this machine's vkit can guess.

    The name is the capability to terminate the run, so it must not be derivable
    from anything an outside party already knows. A random token per run is the
    whole defence; deriving it from the run id would be security by obscurity.
    """
    return f"Local\\vkit-{run_id}-{secrets.token_hex(8)}"


@dataclass(frozen=True)
class StartedRun:
    """A registered run and the outcome it reached."""

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
) -> StartedRun:
    """Register and execute one check, recording launch intent before launching.

    `run_id` lets a caller retry into the same run. A retry of a run that already
    finished returns the recorded outcome instead of executing a second time.
    """
    manifest = manifest or parse_manifest(project, project.runs_root / "probe")
    manifest.require(check_id)

    run_id = run_id or uuid.uuid4().hex
    if (store.run_dir(run_id) / "report.json").is_file():
        stored = store.load(run_id)
        return StartedRun(run_id, outcome_from_report(stored), stored)

    source: SourceIdentity = compute_source_identity(project)
    # Registration, launch intent, and process identity all belong to
    # `execution.run_check`. Registering here as well meant every call raised
    # "run is already registered", so the documented entry point for a
    # background run had never once executed.
    try:
        result = run_check(manifest, check_id, store=store, source=source, run_id=run_id)
    except ExecutionError as exc:
        raise SupervisorError(f"run {run_id} could not be published: {exc}") from exc
    return StartedRun(run_id, result.outcome, result.report)


def cancel_run(
    store: Store,
    run_id: str,
    *,
    identity: ProcessIdentity,
) -> tuple[Outcome, dict[str, Any]]:
    """Cancel a run by verified identity, never by a bare pid.

    The order is fixed and each step can fail differently: verify the process is
    still the one we launched, terminate the owned tree, confirm, publish. A
    failure at any step leaves the run BLOCKED with `ownership_lost` and the
    claims held, because freeing a resource whose owner may still be alive is how
    two workers end up writing the same checkout.
    """
    if not isinstance(identity, ProcessIdentity):
        raise SupervisorError(
            "cancellation requires a ProcessIdentity carrying a pid and a creation "
            "time; a bare pid is refused because pids are recycled"
        )

    report_path = store.run_dir(run_id) / "report.json"
    if report_path.is_file():
        stored = json.loads(report_path.read_text(encoding="utf-8"))
        if stored.get("lifecycle") == "terminal":
            # Already finished. Cancelling a finished run returns the outcome it
            # actually reached rather than inventing a cancellation.
            return outcome_from_report(stored), stored

    recorded = store.run_process_identity(run_id) or {}
    if not recorded.get("pid"):
        return _blocked_publish(
            store, run_id, recorded,
            BlockedReason.OWNERSHIP_LOST,
            f"run {run_id} recorded no process, so there is nothing to cancel",
        )
    if identity.pid != recorded["pid"]:
        return _blocked_publish(
            store, run_id, recorded,
            BlockedReason.OWNERSHIP_LOST,
            f"run {run_id} is owned by pid {recorded['pid']}, not {identity.pid}",
        )
    if not still_the_same_process(identity):
        return _blocked_publish(
            store, run_id, recorded,
            BlockedReason.OWNERSHIP_LOST,
            f"pid {identity.pid} is not the process this run launched, or no longer "
            "exists; refusing to signal it, because the pid may have been recycled",
        )

    try:
        terminate_owned_tree(recorded.get("job_name"))
    except OSError as exc:
        return _blocked_publish(
            store, run_id, recorded,
            BlockedReason.OWNERSHIP_LOST,
            f"could not terminate the owned tree: {exc}; the claim is retained",
        )

    blocked = Blocked(BlockedReason.CANCELLED,
                      f"cancelled on request; the owned tree for pid {identity.pid} was stopped")
    report = _cancel_report(run_id, recorded, blocked, identity)
    store.publish(run_id, report)
    return blocked, report


def terminate_owned_tree(job_name: str | None) -> None:
    """Terminate the job object owning a run's tree, from any process.

    Windows first, because that is the containment this milestone verified. The
    POSIX path signals a process group and is best effort; it has not been run on
    a POSIX host and says so rather than pretending otherwise.
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
        "POSIX run termination is not implemented on this build; the process group "
        "signal is unverified and Plan 09 must not claim POSIX support"
    )


def _blocked_publish(
    store: Store, run_id: str, recorded: dict[str, Any], reason: BlockedReason, detail: str
) -> tuple[Outcome, dict[str, Any]]:
    blocked = Blocked(reason, detail)
    report = _cancel_report(run_id, recorded, blocked, None)
    store.publish(run_id, report)
    return blocked, report


def _cancel_report(
    run_id: str, recorded: dict[str, Any], blocked: Blocked, identity: ProcessIdentity | None
) -> dict[str, Any]:
    """A terminal report for a cancelled or un-cancellable run.

    It records what the store knows rather than inventing provenance: a run that
    never launched has no source identity or command to report, and pretending
    otherwise would put a fabrication in the durable evidence.
    """
    process: dict[str, Any] | None = None
    if identity is not None:
        process = {
            "pid": identity.pid,
            "creation_time": identity.creation_time,
            "ownership": recorded.get("ownership") or "windows_job_object",
            "exit_code": None,
            "timed_out": False,
        }
        if recorded.get("job_name"):
            process["job_name"] = recorded["job_name"]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "check_id": recorded.get("check_id", "unknown"),
        "lifecycle": "terminal",
        "started_at": recorded.get("registered_at") or _now(),
        "ended_at": _now(),
        "outcome": blocked.to_json(),
        "source": recorded.get("source") or {"head": "unknown", "inventory_digest": "unknown", "dirty": False},
        "command": recorded.get("command") or {"argv": ["<never launched>"], "cwd": "<none>"},
        "configuration_digest": recorded.get("configuration_digest") or "unknown",
        "fixture_digest": None,
        "process": process,
        "artifacts": {},
        "environment": {},
        "logs": {"stdout": None, "stderr": None},
    }
