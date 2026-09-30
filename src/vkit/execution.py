"""Execute one registered check and publish a durable report.

This is the seam Plan 02 consumes. The per-run supervisor calls
`run_check`; it must never shell out to the CLI to do ordinary internal work, so
the CLI below is a thin parser around this function and not the other way round.

Outcome derivation is the substance of the module, so it is one function with
one job: given what the process did and what the artifact says, return exactly
one Outcome. The ordering of the checks below is the contract. A process that
exited zero but wrote no artifact is BLOCKED, never PASS, because a silent
success is the failure mode this whole product exists to catch.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from .identity import SourceIdentity, compute_source_identity, source_unchanged
from .manifest import CheckSpec, Manifest, Prerequisite
from .outcome import (
    Blocked,
    BlockedReason,
    Failed,
    Outcome,
    Passed,
    ScenarioResult,
)
from .procs import run_command
from .schemas import CHECK_ARTIFACT, RUN_REPORT, SchemaValidationError, parse_artifact, validate
from .storage import Store, StoreError


class ExecutionError(Exception):
    """The run could not be set up at all. Distinct from a BLOCKED outcome, which
    is a real, recorded answer about the check rather than a failure to try."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_prerequisites(check: CheckSpec) -> Blocked | None:
    """Report a missing prerequisite without launching anything.

    Imported lazily so the prerequisite probe costs nothing on the happy path and
    so a POSIX host never has to import the Windows module at all.
    """
    import shutil

    for need in check.prerequisites:
        found = shutil.which(need.executable)
        if found is None:
            return Blocked(
                BlockedReason.PREREQUISITE_MISSING,
                f"{need.name}: {need.executable!r} is not on PATH",
            )
    return None


@dataclass(frozen=True)
class RunOutcome:
    """What a caller needs: the recorded report, and the outcome to exit on."""

    report: dict[str, Any]
    outcome: Outcome


def _scenarios_from_artifact(
    raw: bytes, required: tuple[str, ...]
) -> tuple[tuple[ScenarioResult, ...], Blocked | None]:
    """Decode and validate the artifact the check wrote.

    Returns scenarios, or a Blocked carrying the reason. Kept apart from the
    caller so every failure path here is a value rather than an exception, and
    the reason is always specific.
    """
    try:
        document = parse_artifact(raw)
    except SchemaValidationError as exc:
        return (), Blocked(BlockedReason.ARTIFACT_MALFORMED, exc.reason)

    try:
        validate("check artifact", CHECK_ARTIFACT, document)
    except SchemaValidationError as exc:
        return (), Blocked(BlockedReason.ARTIFACT_MALFORMED, exc.reason)

    reported = document["scenarios"]
    if not reported:
        return (), Blocked(
            BlockedReason.ARTIFACT_EMPTY, "the artifact listed no scenarios, which is not evidence"
        )

    scenarios = tuple(
        ScenarioResult(item["id"], item["result"] == "PASS", item["observation"])
        for item in reported
    )
    known = {s.scenario_id for s in scenarios}
    missing = [s for s in required if s not in known]
    if missing:
        return scenarios, Blocked(
            BlockedReason.SCENARIO_UNKNOWN,
            f"the artifact never reported required scenario(s): {', '.join(missing)}",
        )
    return scenarios, None


@dataclass(frozen=True)
class ProcessResult:
    """What the execution layer reports, in the shape the outcome rules read.

    `launch_error` is a (reason, detail) pair rather than a bare string so a
    refusal to own the process stays distinguishable from a command that ran and
    failed. A run must never claim a subprocess executed when none did.
    """

    pid: int | None
    ownership: str
    exit_code: int | None
    timed_out: bool
    launch_error: tuple[BlockedReason, str] | None = None


def _derive(
    check: CheckSpec,
    process: ProcessResult,
    artifact: Path | None,
) -> Outcome:
    """Decide the outcome from what actually happened.

    Every branch returns a BLOCKED with a specific reason except the two that
    have positive evidence. Process success alone is never enough and artifact
    validity alone is never enough; both are required, per CONTRACT.md.
    """
    if process.launch_error is not None:
        reason, detail = process.launch_error
        return Blocked(reason, detail)
    if process.timed_out:
        return Blocked(
            BlockedReason.TIMEOUT,
            f"exceeded {check.timeout_seconds:g}s; owned descendants were stopped",
        )
    if artifact is None or not artifact.is_file():
        return Blocked(
            BlockedReason.ARTIFACT_MISSING,
            f"{check.artifact_name} was not written, so the check reported nothing",
        )

    scenarios, problem = _scenarios_from_artifact(artifact.read_bytes(), check.required_scenarios)
    if problem is not None:
        return problem
    if process.exit_code != 0:
        return Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"the check exited {process.exit_code} but still wrote an artifact; "
            "the artifact cannot be trusted from a failed run",
        )

    failing = tuple(s for s in scenarios if not s.passed)
    if failing:
        return Failed(scenarios)
    return Passed(scenarios)


def _environment_facts(check: CheckSpec) -> dict[str, Any]:
    """Only the facts a reader needs to reproduce the run. Never the full
    environment, and never a credential."""
    import platform
    import shutil
    import subprocess
    import sys

    tools: dict[str, str] = {}
    git = shutil.which("git")
    if git:
        try:
            done = subprocess.run(
                [git, "--version"], capture_output=True, encoding="utf-8",
                errors="replace", timeout=15,
            )
            tools["git"] = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "tool_versions": tools,
    }


def run_check(
    manifest: Manifest,
    check_id: str,
    *,
    store: Store,
    source: SourceIdentity,
    run_id: str | None = None,
) -> RunOutcome:
    """Register, execute, and publish one run of one registered check.

    Callable directly by the CLI and, in Plan 02, by the per-run supervisor.
    Never invokes the CLI.
    """
    check = manifest.require(check_id)
    run_id = run_id or uuid.uuid4().hex
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Register before anything else can conclude. A missing prerequisite is a
    # real answer about this run and has to be recorded as one, and a report can
    # only be published against a row that exists.
    store.register_run(
        run_id, check.id, task_id=None, attempt=None,
        source=source.to_json(), configuration_digest=manifest.digest(),
        fixture_digest=None,
    )

    blocked = check_prerequisites(check)
    if blocked is not None:
        report = _terminal_report(
            run_id, check, manifest, source,
            outcome=blocked, started_at=_now(), ended_at=_now(),
            process=None, artifact=None, run_dir=run_dir,
        )
        _publish(store, run_id, report)
        return RunOutcome(report, blocked)

    started_at = _now()
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    # The interpreter a check should use, and the run directory it should write
    # into, are the only two substitutions. The manifest may not name a shell.
    argv = check.resolved_argv(run_dir, sys.executable)
    result = run_command(
        list(argv),
        cwd=check.cwd,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=check.timeout_seconds,
    )
    process = ProcessResult(
        pid=result.pid,
        ownership=result.ownership,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        launch_error=None if result.reason is None else (result.reason, result.detail),
    )
    store.mark_running(run_id, {
        "pid": process.pid, "ownership": process.ownership,
        "exit_code": process.exit_code, "timed_out": process.timed_out,
    })

    artifact = store.resolve_artifact(run_id, check.artifact_name)
    outcome = _derive(check, process, artifact if artifact.is_file() else None)

    after = compute_source_identity(manifest.project)
    if not source_unchanged(source, after):
        outcome = Blocked(
            BlockedReason.SOURCE_CHANGED,
            "the source changed while the check was running, so the evidence does "
            "not describe the code that is now on disk",
        )

    report = _terminal_report(
        run_id, check, manifest, source,
        outcome=outcome, started_at=started_at, ended_at=_now(),
        process=process, artifact=artifact, run_dir=run_dir,
        executed_argv=list(argv),
    )
    _publish(store, run_id, report)
    return RunOutcome(report, outcome)


def _terminal_report(
    run_id: str,
    check: CheckSpec,
    manifest: Manifest,
    source: SourceIdentity,
    *,
    outcome: Outcome,
    started_at: str,
    ended_at: str,
    process: ProcessResult | None,
    artifact: Path | None,
    run_dir: Path,
    executed_argv: list[str] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "check_id": check.id,
        "lifecycle": "terminal",
        "started_at": started_at,
        "ended_at": ended_at,
        "outcome": outcome.to_json(),
        "source": source.to_json(),
        # The list actually executed, so a reader can reproduce the run without
        # knowing what the placeholders meant.
        "command": {
            "argv": executed_argv if executed_argv is not None else list(check.argv),
            "cwd": str(check.cwd),
        },
        "configuration_digest": manifest.digest(),
        "fixture_digest": None,
        # A refused launch produces a real result whose pid is None, not a
        # missing process. Guarding on the object left process.pid = None in the
        # report, which violates the schema, so publish never ran and the row
        # stayed 'running' with no result forever. A run that never started has
        # no process to record.
        "process": None if process is None or process.pid is None else {
            "pid": process.pid,
            "ownership": process.ownership,
            "exit_code": process.exit_code,
            "timed_out": process.timed_out,
        },
        "artifacts": {},
        "environment": _environment_facts(check),
        "logs": {"stdout": "stdout.log", "stderr": "stderr.log"},
    }
    if artifact is not None and artifact.is_file():
        # Run-relative, so the reference survives the run directory being moved
        # and never points at a worker's temporary checkout.
        report["artifacts"] = {"result": artifact.name}
    try:
        validate("run report", RUN_REPORT, report)
    except SchemaValidationError as exc:
        raise ExecutionError(f"generated a report that violates its own schema: {exc.reason}") from exc
    return report


def _publish(store: Store, run_id: str, report: dict[str, Any]) -> None:
    try:
        store.publish(run_id, report)
    except StoreError as exc:
        raise ExecutionError(f"could not publish the report for {run_id}: {exc}") from exc
