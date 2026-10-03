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

import os
import uuid
from contextlib import contextmanager
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
from .verifiers import dispatch
from .procs import await_exit, launch, run_command
from .schemas import CHECK_ARTIFACT, RUN_REPORT, SchemaValidationError, parse_artifact, validate
from .storage import RECEIPT_NAME, Store, StoreError


class ExecutionError(Exception):
    """The run could not be set up at all. Distinct from a BLOCKED outcome, which
    is a real, recorded answer about the check rather than a failure to try."""


@contextmanager
def _this_package_importable():
    """Put the running vkit's own source on a launched child's import path.

    A `pytest` check loads vkit's report plugin into the runner, and the runner
    is a separate process whose `sys.path` comes from this interpreter's
    environment rather than from this checkout. An editable install puts ONE
    absolute `src` on the path through a `.pth`, so a runner launched from a
    worktree resolves the installed copy and cannot import the module under test.
    Measured here: the child failed with
    `No module named 'vkit.verifiers.pytest_adapter'` and wrote no report at all,
    which surfaced as `artifact_missing` and named the wrong cause.

    This is the same device `supervisor._spawn_supervisor` uses to pin a
    detached supervisor's `PYTHONPATH`, for the same reason. It goes through
    `os.environ` because `procs` accepts no environment and is not this unit's
    file, and the previous value is restored on the way out so a caller running
    two checks cannot inherit the first one's path into the second.

    Only `PYTHONPATH` is touched. Every other environment fact the child sees is
    the one this process had, which is the ordinary meaning of a local run.
    """
    root = str(Path(__file__).resolve().parents[1])
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = (
        root if not previous else os.pathsep.join([root, previous])
    )
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous


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
) -> tuple[Outcome, Any]:
    """Decide the outcome from what actually happened, and read what was written.

    Every branch returns a BLOCKED with a specific reason except the two that
    have positive evidence. Process success alone is never enough and artifact
    validity alone is never enough; both are required, per CONTRACT.md.

    Returns the outcome together with the adapter's reading, because the receipt
    is a projection of the reading and an outcome that could not be derived
    without it would have had to re-derive it. The two travel together so no
    caller can build a receipt from a verdict it did not read the evidence for.

    The reading is dispatched on the check's kind. For a `scenario` check that is
    the v1 artifact parser unchanged; for every other kind it is the interpreter
    the check's variant names, which is what stops the core scraping success
    prose out of a driver's output the way `plans/CONTRACT.md:58` forbids.
    """
    if process.launch_error is not None:
        reason, detail = process.launch_error
        return Blocked(reason, detail), None
    if process.timed_out:
        return Blocked(
            BlockedReason.TIMEOUT,
            f"exceeded {check.timeout_seconds:g}s; owned descendants were stopped",
        ), None
    if artifact is None or not artifact.is_file():
        return Blocked(
            BlockedReason.ARTIFACT_MISSING,
            f"{check.artifact_name} was not written, so the check reported nothing",
        ), None

    reading = dispatch.interpret(check, artifact.read_bytes())
    reading_outcome = dispatch.outcome_from_reading(reading)
    if isinstance(reading_outcome, Blocked):
        return reading_outcome, None
    if isinstance(reading_outcome, Failed):
        # A nonzero exit is the ordinary shape of a FAIL, not evidence against
        # the artifact. The runner reports "a test failed" in its exit code and
        # names the failing test in its report, and refusing a run whose exit
        # code is nonzero would make every real failure unreachable: the one
        # exit code that means "the runner could not answer" is 4, which the
        # adapter's own refusals have already covered above.
        return reading_outcome, reading
    if process.exit_code not in (0, 1):
        return Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"the check exited {process.exit_code} but still wrote an artifact; "
            "the artifact cannot be trusted from a run the runner could not finish",
        ), None
    return reading_outcome, reading


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
    # For every kind but `scenario` the argv is the adapter's, which is what stops
    # a manifest-declared command from being where the interpreter's argv comes
    # from. `manifest._wrap` already leaves a non-scenario check's own argv empty,
    # so there is no second spelling of a command to disagree with.
    argv = dispatch.argv_for(check, run_dir, env.python)
    with _this_package_importable():
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
    outcome, reading = _derive(check, process, artifact if artifact.is_file() else None)

    if env.revalidate:
        problem = _revalidate(run_dir, check, manifest, env)
        if problem is not None:
            outcome, reading = problem, None

    after = compute_source_identity(manifest.project)
    if not source_unchanged(source, after):
        outcome = Blocked(
            BlockedReason.SOURCE_CHANGED,
            "the source changed while the check was running, so the evidence does "
            "not describe the code that is now on disk",
        )
        reading = None

    report = _terminal_report(
        run_id, check, manifest, source,
        outcome=outcome, started_at=started_at, ended_at=_now(),
        process=process, artifact=artifact, run_dir=run_dir,
        executed_argv=list(argv),
        provenance=env.provenance(),
        fixture_digest=fixture_digest,
        receipt=_write_receipt(
            run_dir, check, manifest, source, run_id, task_id, attempt,
            outcome, reading, artifact, fixture_digest, env,
        ),
    )
    _publish(store, run_id, report)
    return RunOutcome(report, outcome)


def _write_receipt(
    run_dir: Path,
    check: CheckSpec,
    manifest: Manifest,
    source: SourceIdentity,
    run_id: str,
    task_id: str | None,
    attempt: int | None,
    outcome: Outcome,
    reading: Any,
    artifact: Path,
    fixture_digest: str | None,
    env: RunEnvironment,
) -> str | None:
    """Write the typed receipt for this run and return its run-relative name.

    Written for every terminal run, not only a passing one. A FAIL and a BLOCKED
    are recorded evidence too, and a receipt is the only place the category, the
    checked cases and the identities they were checked against travel together;
    a reader holding only a report would have to reconstruct them.

    The name is returned rather than composed by the caller so there is one place
    that decides what a receipt is called, which is the same reason `_terminal_report`
    owns the report's own name in `storage.REPORT_NAME`.

    Returns None only when the run never produced an artifact to be about, which
    is the one case where there is nothing for a receipt to describe.
    """
    if not artifact.is_file():
        return None
    subject = check.subject or verifiers.SubjectRef()
    receipt = dispatch.build_receipt(dispatch.ReceiptInputs(
        check_id=check.id,
        claim_id=check.claim_id or check.id,
        run_id=run_id,
        task_id=task_id,
        generation=attempt,
        category=check.evidence_kind(),
        subject=subject,
        # No check declares a specification file at checkpoint 1, so this is
        # null rather than a digest of something nothing named.
        specification_digest=None,
        policy_digest=manifest.digest(),
        source=source,
        fixture_digest=fixture_digest,
        tool_versions=_environment_facts(check)["tool_versions"],
        runtime={
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "requires_os": "any",
        },
        timeout_seconds=check.timeout_seconds,
        report_path=artifact,
        project_root=manifest.project.root,
        outcome=outcome,
        reading=reading,
    ))
    name = RECEIPT_NAME
    dispatch.persist(receipt, run_dir / name)
    return name


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
    receipt: str | None = None,
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
    if receipt is not None:
        # The receipt is referenced rather than inlined, which is what keeps the
        # run report describing the process while the receipt describes the
        # evidence. `artifacts` is an open map of run-relative names in
        # `run-report.v1.json`, so a second entry needs no schema version: the
        # report gains a pointer, not a field.
        report["artifacts"] = {**report["artifacts"], "receipt": receipt}
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
