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
from .procs import await_exit, launch, run_command
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
    creation_time: int | None = None
    #: POSIX only, empty on Windows. Part of the recorded identity, because a
    #: start time is ticks since boot and is therefore only comparable within
    #: one boot. See vkit.procidentity.
    boot_id: str = ""


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

    from .nowindow import hidden_window

    tools: dict[str, str] = {}
    # `{{python}}` is the only way a check can name an interpreter, so the tool
    # that ran the checks is part of the evidence. A check that substituted some
    # other interpreter is not reproducible from this report without it.
    for name, argv in (("git", ["git", "--version"]), ("python", [sys.executable, "--version"])):
        resolved = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else None)
        if not resolved:
            continue
        try:
            done = subprocess.run(
                [resolved, *argv[1:]], capture_output=True, encoding="utf-8",
                errors="replace", timeout=15, **hidden_window(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        tools[name] = done.stdout.strip() or done.stderr.strip()
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "tool_versions": tools,
    }


@dataclass(frozen=True)
class RunEnvironment:
    """How a check is executed, for callers that must not use the ambient one.

    A development run is the ambient environment: this interpreter, and no
    instrumentation. That is the right default and it is why this type exists
    only to be overridden.

    Plan 07's trusted integration path overrides it, and the reason is
    containment rather than convenience. A candidate's own verifier code runs the
    candidate's checks, inside a checkout the candidate cannot write, and every
    report the candidate's code produces is re-validated against the
    authoritative schemas by a process the candidate does not control. A claim
    that a candidate could edit its own report into a PASS is exactly the
    failure this whole product exists to prevent, so the boundary is stated here
    rather than assembled by a caller.
    """

    python: str | None = None
    plugin_root: Path | None = None
    revalidate: bool = False
    verifier_revision: str | None = None

    @property
    def is_ambient(self) -> bool:
        return self.python is None and self.plugin_root is None and not self.revalidate

    def provenance(self) -> dict[str, str]:
        if self.is_ambient:
            return {"mode": "ambient"}
        facts = {
            "mode": "trusted_integration",
            "verifier_revision": self.verifier_revision or "unknown",
            "python": self.python or "inherited",
            "artifact_capture": "verifier" if self.plugin_root is not None else "verifier",
            "artifact_revalidated": "yes" if self.revalidate else "no",
        }
        if self.plugin_root is not None:
            facts["plugin_root"] = str(self.plugin_root)
        return facts


AMBIENT = RunEnvironment()


def run_check(
    manifest: Manifest,
    check_id: str,
    *,
    store: Store,
    source: SourceIdentity,
    run_id: str | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    env: RunEnvironment = AMBIENT,
) -> RunOutcome:
    """Register, execute, and publish one run of one registered check.

    Callable directly by the CLI and, in Plan 02, by the per-run supervisor.
    Never invokes the CLI.
    """
    check = manifest.require(check_id)
    fixture = manifest.fixture_identity()
    fixture_digest = None if fixture is None else fixture.digest
    run_id = run_id or uuid.uuid4().hex
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Register before anything else can conclude. A missing prerequisite is a
    # real answer about this run and has to be recorded as one, and a report can
    # only be published against a row that exists. The task and attempt are
    # carried here rather than bound afterwards, because the caller that knows them
    # is the one that decides whether this run counts as evidence for the attempt,
    # and leaving the column null would silently remove the run from that attempt's
    # readiness. `Store.attach_task` is therefore unused in the tree and goes with
    # the last caller in `scripts/acceptance02.py`.
    store.register_run(
        run_id, check.id, task_id=task_id, attempt=attempt,
        source=source.to_json(), configuration_digest=manifest.digest(),
        fixture_digest=fixture_digest,
    )

    blocked = check_prerequisites(check)
    if blocked is not None:
        report = _terminal_report(
            run_id, check, manifest, source,
            outcome=blocked, started_at=_now(), ended_at=_now(),
            process=None, artifact=None, run_dir=run_dir,
            provenance=env.provenance(),
            fixture_digest=fixture_digest,
        )
        _publish(store, run_id, report)
        return RunOutcome(report, blocked)

    started_at = _now()
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    # The interpreter a check should use, and the run directory it should write
    # into, are the only two substitutions. The manifest may not name a shell.
    argv = check.resolved_argv_for(run_dir, env.python)
    if env.plugin_root is None:
        # Launched and waited separately, so the identity is durable while the
        # check is still running. The earlier version called `run_command`, which
        # blocks until the command exits, and only then wrote the pid: so for the
        # whole run the durable record said `preparing` with no process, which is
        # indistinguishable from a run that never launched. Recovery read that as
        # "nothing is running" and released the task's claim while this process was
        # still writing.
        lease = launch(
            argv, cwd=check.cwd, stdout_path=stdout_path, stderr_path=stderr_path,
            timeout_seconds=check.timeout_seconds,
        )
        try:
            if lease.pid is not None:
                store.publish_identity(run_id, lease.identity())
            result = await_exit(lease)
        finally:
            lease.close()
    else:
        result = _launch(argv, check, stdout_path, stderr_path, env)
        if result.pid is not None:
            store.publish_identity(run_id, {
                "pid": result.pid,
                "creation_time": result.creation_time,
                **({"boot_id": result.boot_id} if result.boot_id else {}),
                "ownership": result.ownership,
            })
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

    if env.revalidate:
        problem = _revalidate(run_dir, check, manifest, env)
        if problem is not None:
            outcome = problem

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
        provenance=env.provenance(),
        fixture_digest=fixture_digest,
    )
    _publish(store, run_id, report)
    return RunOutcome(report, outcome)


def _launch(
    argv: list[str],
    check: CheckSpec,
    stdout_path: Path,
    stderr_path: Path,
    env: RunEnvironment,
):
    """Run the check, with the check's own code isolated when the caller asked.

    Without a plugin root this is the ordinary launch, so a development run is
    unchanged. With one, the candidate's driver runs as a grandchild of this
    process, with its own `import vkit` refused, and its artifact copied out by
    the launcher. `vkit/integration/sandbox.py` and `launcher.py` hold that
    contract; they are the counterpart of this branch and change with it.
    """
    if env.plugin_root is None:
        return run_command(
            argv, cwd=check.cwd, stdout_path=stdout_path,
            stderr_path=stderr_path, timeout_seconds=check.timeout_seconds,
        )
    # Imported here so the development path never pays for the integration
    # package, and so a host without it can still run every ordinary check.
    from .integration.sandbox import run_in_plugin_subprocess

    return run_in_plugin_subprocess(
        argv, cwd=check.cwd, stdout_path=stdout_path, stderr_path=stderr_path,
        timeout_seconds=check.timeout_seconds, plugin_root=env.plugin_root,
        capture_artifacts=(check.artifact_name,),
    )


def _revalidate(run_dir: Path, check: CheckSpec, manifest: Manifest, env: RunEnvironment) -> Blocked | None:
    """Validate the bytes the trusted launcher captured, and compare the copies.

    The captured copy is authoritative: it was taken after the last process to
    write the artifact had exited, by a process the candidate does not control.
    It is then validated against this package's own schemas, and finally
    compared with what the candidate left behind. A candidate that edited its own
    artifact in place after producing it has left a disagreement, and that is a
    BLOCKED reason rather than a pass.
    """
    from .integration.sandbox import disagreement, read_artifacts, validate_artifact_bytes

    try:
        captured, submitted = read_artifacts(run_dir, check.artifact_name)
    except FileNotFoundError as exc:
        return Blocked(BlockedReason.ARTIFACT_MISSING, str(exc))

    problem = validate_artifact_bytes(
        captured, check=check, manifest=manifest, env=env
    )
    if problem is not None:
        return problem
    return disagreement(captured, submitted)


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
    provenance: dict[str, str] | None = None,
    fixture_digest: str | None = None,
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
        "fixture_digest": fixture_digest,
        # A refused launch produces a real result whose pid is None, not a
        # missing process. Guarding on the object records null here, and null is
        # what the schema accepts: `process.pid` is declared an integer, so a
        # `process` object carrying a null pid is the shape the schema refuses.
        # A run that never started has no process to record.
        "process": None if process is None or process.pid is None else {
            "pid": process.pid,
            **({"creation_time": process.creation_time}
               if process.creation_time is not None else {}),
            **({"boot_id": process.boot_id} if process.boot_id else {}),
            "ownership": process.ownership,
            "exit_code": process.exit_code,
            "timed_out": process.timed_out,
        },
        "artifacts": {},
        "environment": _environment_facts(check),
        # What produced this run. Ambient is the default and is what a
        # development run records; the trusted integration launcher names the
        # verifier revision and capture root it used instead. Kept in the
        # report rather than in the acceptance record, because a run is
        # evidence on its own and a reader who only has the report still needs
        # to know who ran it.
        "provenance": provenance or {"mode": "ambient"},
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
