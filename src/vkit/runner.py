"""Run one registered check and record what it established."""

from __future__ import annotations

import os
import hashlib
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import proc
from .budgets import evaluate
from .evidence import backend_identity, seal
from .formal.policy import FormalComponent, GitCommit, VerificationRequest, WorkingTree
from .inputs import (
    CapturedSource,
    InputSet,
    Snapshot,
    canonical_digest,
    capture_git_commit,
    capture_working_tree,
    file_digest,
    load_capture,
    matches,
)
from .job import FormalGoal, RegisteredCheck, seal_job, validate_job
from .manifest import Manifest, parse_check_definition
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed
from .paths import Project
from .store import Store, new_run_id, now, read_json, write_json
from .verifiers import dispatch, static_adapter
from .verifiers.dispatch import ScenarioReading
from .verifiers.spec import CheckSpec, ExternalCheck

_VKIT_SRC = str(Path(__file__).resolve().parents[1])


def missing_prerequisite(check: CheckSpec) -> Blocked | None:
    for need in check.prerequisites:
        if shutil.which(need.executable) is None:
            return Blocked(
                BlockedReason.PREREQUISITE_MISSING,
                f"{need.name}: {need.executable!r} is not on PATH",
            )
    return None


def _child_env() -> dict[str, str]:
    previous = os.environ.get("PYTHONPATH")
    paths = dict.fromkeys(
        [_VKIT_SRC, *(previous.split(os.pathsep) if previous else [])]
    )
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


def _derive(
    check: CheckSpec, exit: proc.Exit, artifact: Path, baseline: dict[str, Any] | None
) -> tuple[Outcome, Any]:
    if exit.launch_error is not None:
        return Blocked(BlockedReason.LAUNCH_FAILED, exit.launch_error), None
    if exit.cancelled:
        return Blocked(
            BlockedReason.CANCELLED, "stopped on request; descendants were stopped"
        ), None
    if exit.timed_out:
        return Blocked(
            BlockedReason.TIMEOUT,
            f"exceeded {check.timeout_seconds:g}s; descendants were stopped",
        ), None
    if not artifact.is_file() or artifact.is_symlink():
        return Blocked(
            BlockedReason.ARTIFACT_MISSING, f"{check.artifact_name} was not written"
        ), None
    reading = dispatch.interpret(check, artifact.read_bytes())
    outcome = dispatch.outcome_from_reading(reading, baseline)
    if isinstance(outcome, Blocked):
        return outcome, None
    if isinstance(outcome, Failed):
        return outcome, reading
    if exit.code not in (0, 1) or (
        check.kind.value in ("lean", "tlc") and exit.code != 0
    ):
        return Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"the check exited {exit.code} after writing an artifact, so the artifact is not trusted",
        ), None
    return outcome, reading


def _apply_budgets(
    check: CheckSpec, outcome: Outcome, reading: Any, baseline: dict[str, Any] | None
) -> tuple[Outcome, tuple[str, ...]]:
    if not isinstance(reading, ScenarioReading) or isinstance(outcome, Blocked):
        return outcome, ()
    results, unchecked = evaluate(
        getattr(check, "budgets", ()),
        reading.measurements,
        (baseline or {}).get("measurements"),
    )
    if not results:
        return outcome, unchecked
    scenarios = outcome.scenarios + results
    return (
        Failed(scenarios) if any(not r.passed for r in scenarios) else Passed(scenarios)
    ), unchecked


def _measurements(
    reading: Any, baseline: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not isinstance(reading, ScenarioReading):
        return []
    pinned = (baseline or {}).get("measurements", {})
    return [
        {**m.to_json(), "baseline": pinned.get(m.name)} for m in reading.measurements
    ]


def _findings(reading: Any) -> list[dict[str, Any]]:
    return (
        [f.to_json() for f in reading.findings]
        if isinstance(reading, static_adapter.StaticReading)
        else []
    )


def _obligations(reading: Any) -> dict[str, Any] | None:
    if reading is None or not hasattr(reading, "obligation_results"):
        return None
    satisfied, counterexamples = reading.obligation_results()
    return {"satisfied": list(satisfied), "counterexamples": list(counterexamples)}


def _external_logical_trust(
    check: ExternalCheck, metadata: dict[str, Any]
) -> dict[str, Any]:
    identity = metadata.get("identity") or {}
    backend = identity.get("backend") or {}
    checker = identity.get("checker") or {}
    checker_executable = checker.get("executable") or {}
    receipt = metadata.get("checker_receipt") or {}
    receipt_document = receipt.get("document") or {}
    independent_status = {"accepted": "checked", "rejected": "rejected", "unknown": "unknown"}.get(
        receipt_document.get("verdict"), "not_provided")
    pinned_dependencies = []
    for role, command in (("producer", backend), ("checker", checker)):
        if isinstance(command.get("executable"), dict):
            pinned_dependencies.append({"role": role, **command["executable"]})
        for dependency in command.get("dependencies", []):
            pinned_dependencies.append({"role": role, **dependency})
    pinned_dependencies.extend(identity.get("trusted_tool_dependencies", []))
    return {
        "schema_version": 1,
        "engine": {
            "name": Path((backend.get("executable") or {}).get("path", "external")).name,
            "version": None,
        },
        "scope": {
            "kind": "external_specification",
            "items": [{
                "model_scope": check.model_scope,
                "specification": metadata.get("specification"),
                "obligations": metadata.get("obligations", list(check.obligations)),
            }],
            "assumptions": {"status": "policy_bound", "items": list(check.assumptions)},
        },
        "dependencies": {
            "status": "policy_bound" if pinned_dependencies else "not_reported",
            "items": pinned_dependencies,
        },
        "axioms": {
            "status": "not_reported",
            "by_obligation": [
                {"obligation": str(name), "status": "not_reported", "items": []}
                for name in check.obligations
            ],
        },
        "independent_check": {
            "status": independent_status,
            "checker": ({
                "name": Path(checker_executable.get("path", "checker")).name,
                "version": None,
                "digest": checker_executable.get("sha256"),
            } if receipt_document else None),
            "certificate": None,
        },
    }


def _record_logical_trust(
    check: CheckSpec, reading: Any, metadata: dict[str, Any],
    run_dir: Path, record: dict[str, Any],
) -> None:
    reported = getattr(reading, "logical_trust", None)
    if isinstance(reported, dict):
        metadata["logical_trust"] = reported
    elif isinstance(check, ExternalCheck):
        metadata["logical_trust"] = _external_logical_trust(check, metadata)
    if check.kind.value == "lean":
        from .verifiers import lean_adapter

        artifact = run_dir / check.artifact_name
        if not artifact.is_file() or artifact.is_symlink():
            return
        raw = artifact.read_bytes()
        if "logical_trust" not in metadata:
            trust = lean_adapter.reported_logical_trust(raw, check)
            if trust is not None:
                metadata["logical_trust"] = trust
        diagnostics = lean_adapter.source_diagnostics(
            raw, check, record["inputs"].get("files", []), snapshot_root=run_dir / "snapshot")
        if diagnostics:
            metadata["source_diagnostics"] = diagnostics


def prepare(
    project: Project,
    manifest: Manifest,
    check_id: str,
    *,
    store: Store,
    run_id: str | None = None,
    replay_source: Path | None = None,
    replay_receipt: str | None = None,
    source: CapturedSource | None = None,
    authority: dict[str, Any] | None = None,
    backend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an immutable job and a separate queued execution attempt."""
    check = manifest.require(check_id)
    if source is None:
        inputs = Snapshot(project).inputs(check.inputs)
        source = capture_working_tree(project, [name for name, _ in inputs.files])
    else:
        chosen = [f for f in source.files if not check.inputs or any(matches(f.path, p) for p in check.inputs)]
        inputs = InputSet(check.inputs, tuple((f.path, hashlib.sha256(f.content.replace(b"\r\n", b"\n")).hexdigest()) for f in chosen),
                          tuple(p for p in check.inputs if not any(matches(f.path, p) for f in source.files)))
    baseline = None if authority else store.baselines().get(check_id)
    backend = backend if backend is not None else backend_identity(check)
    job = seal_job(
        RegisteredCheck.resolve(
            manifest, check_id, baseline, backend, inputs.to_json(), source, authority
        ).payload()
    )
    record = _record_for_job(job, run_id or new_run_id())
    record.update(category=check.evidence_kind().value, description=check.description,
                  claim_id=check.claim_id, worktree=str(project.root))
    run_dir = store.run_dir(record["run_id"])
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = run_dir / "snapshot"
    snapshot.mkdir()
    source.materialize(snapshot)
    if replay_source is not None:
        from .evidence import validate_record

        retained = run_dir / "replay"
        source_record = read_json(replay_source / "record.json")
        if (
            source_record is None
            or source_record["receipt"]["sha256"] != replay_receipt
        ):
            raise ValueError("source receipt changed before replay capture")
        shutil.copytree(
            replay_source,
            retained,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                "lock", "cancel", "worker.log", "worker.json", "replay"
            ),
        )
        if not validate_record(source_record, retained):
            raise ValueError("evidence changed while capturing replay bundle")
        record["replayed_from"] = {
            "run_id": source_record["run_id"],
            "receipt": source_record["receipt"]["sha256"],
        }
    write_json(run_dir / "job.json", job)
    store.save(record)
    return record


def prepare_formal(
    project: Project,
    request: VerificationRequest,
    component: FormalComponent,
    checker_sha256: str,
    *,
    store: Store,
    run_id: str | None = None,
    source: CapturedSource | None = None,
    authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request.component != component.id:
        raise ValueError("verification request and formal component IDs differ")
    if source is not None:
        if not isinstance(request.candidate, GitCommit) or source.commit != request.candidate.oid:
            raise ValueError("provided capture differs from the requested Git candidate")
    elif isinstance(request.candidate, GitCommit):
        source = capture_git_commit(project, request.candidate.oid)
        if request.candidate.target_commit is not None:
            target = request.candidate.target_commit
            if len(target) != len(request.candidate.oid):
                raise ValueError("target commit object format differs from candidate")
    elif isinstance(request.candidate, WorkingTree):
        source = capture_working_tree(project)
    else:
        raise ValueError("unsupported formal candidate source")
    job = seal_job(FormalGoal(request, component, checker_sha256, source, authority).payload())
    record = _record_for_job(job, run_id or new_run_id())
    record.update(category="source_verification", description=component.contract, claim_id=component.id)
    run_dir = store.run_dir(record["run_id"])
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = run_dir / "snapshot"
    snapshot.mkdir()
    source.materialize(snapshot)
    write_json(run_dir / "job.json", job)
    store.save(record)
    return record


def prepare_retry(
    source_run: str, *, store: Store, run_id: str | None = None
) -> dict[str, Any]:
    old_dir = store.run_dir(source_run)
    job = validate_job(read_json(old_dir / "job.json"))
    source = load_capture(job["source"], old_dir / "snapshot")
    run_id = run_id or new_run_id()
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    snapshot = run_dir / "snapshot"
    snapshot.mkdir()
    source.materialize(snapshot)
    if (old_dir / "replay").is_dir():
        shutil.copytree(old_dir / "replay", run_dir / "replay", symlinks=True)
    record = _record_for_job(job, run_id)
    old_record = store.record(source_run)
    if old_record:
        for name in ("category", "description", "claim_id", "worktree"):
            if name in old_record:
                record[name] = old_record[name]
        if old_record.get("replayed_from"):
            record["replayed_from"] = old_record["replayed_from"]
    write_json(run_dir / "job.json", job)
    store.save(record)
    return record


def _record_for_job(job: dict[str, Any], run_id: str) -> dict[str, Any]:
    subject = job["subject"]
    source = job["source"]
    formal = subject["kind"] == "formal_goal"
    inputs = (
        job["inputs"]
        if not formal
        else {
            "digest": source["digest"],
            "scope": f"whole tree ({len(source['files'])} files)",
            "declared": [],
            "files": [
                {"path": item["path"], "sha256": item["raw_sha256"]}
                for item in source["files"]
            ],
            "unmatched": [],
        }
    )
    record: dict[str, Any] = {
        "schema_version": 2,
        "job_id": job["job_id"],
        "key": job["job_id"],
        "subject": subject,
        "source": source,
        "run_id": run_id,
        "inputs": inputs,
        "state": "queued",
        "submitted_at": now(),
        "started_at": None,
        "ended_at": None,
        "outcome": None,
        "obligations": None,
        "measurements": [],
        "argv": None,
        "exit_code": None,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    }
    if "authority" in job:
        record["authority"] = job["authority"]
    if formal:
        for name in ("policy_sha256", "package_sha256", "checker_sha256"):
            record[name] = job[name]
    else:
        record.update(
            check_id=subject["id"],
            check_digest=job["check_digest"],
            backend=job["backend"],
            baseline=job["baseline"],
        )
    return record


def execute(project: Project | None, run_id: str, *, store: Store) -> dict[str, Any]:
    """Claim one attempt. The OS lock prevents two workers from executing it."""
    run_dir = store.run_dir(run_id)
    with store.hold(run_id):
        record = store.record(run_id)
        if record["state"] != "queued":
            return record
        job = validate_job(read_json(run_dir / "job.json"))
        if (
            job["job_id"] != record["job_id"]
            or job["subject"] != record["subject"]
            or job["source"] != record["source"]
            or _record_for_job(job, run_id)["inputs"] != record["inputs"]
            or job["job_id"] != record["key"]
            or job.get("authority") != record.get("authority")
        ):
            raise ValueError("persisted job does not match its queued run record")
        load_capture(job["source"], run_dir / "snapshot")
        if "authority" in job:
            from .ci import validate_authority

            if project is None:
                raise ValueError("target-policy execution requires its trusted host Git repository")
            validate_authority(job, project)
        if record["subject"]["kind"] == "registered_check":
            if (job["check_digest"] != record["check_digest"]
                    or job["backend"] != record["backend"]
                    or job["baseline"] != record["baseline"]):
                raise ValueError("registered-check record does not match its persisted job")
            if project is None and "authority" not in job:
                raise ValueError(
                    "registered-check worker requires its live project for freshness"
                )
            snapshot_project = Project(run_dir / "snapshot", project.git_common_dir if project else store.root)
            check = parse_check_definition(job["definition"], snapshot_project)
            if check.id != record["subject"]["id"] or job[
                "check_digest"
            ] != canonical_digest(job["definition"]):
                raise ValueError(
                    "registered-check definition does not match its persisted identity"
                )
        else:
            if any(job[name] != record[name] for name in (
                "policy_sha256", "package_sha256", "checker_sha256"
            )):
                raise ValueError("formal record does not match its persisted job")
            check = None
        record.update(state="running", started_at=now(), worker_pid=os.getpid())
        store.save(record)
        metadata: dict[str, Any] = {}
        try:
            if check is None:
                outcome, metadata = _execute_formal(job, run_id, run_dir, store)
                reading, exit, unchecked = None, None, ()
            else:
                check.cwd.mkdir(parents=True, exist_ok=True)
                outcome, reading, exit, metadata = _execute(
                    project, check, store, record, job
                )
                outcome, unchecked = _apply_budgets(
                    check, outcome, reading, record["baseline"]
                )
                if "authority" in job and unchecked:
                    outcome = Blocked(BlockedReason.INPUT_MISSING,
                                      "target policy budgets lack a trusted baseline: " + ", ".join(unchecked))
        except Exception as exc:
            outcome = Blocked(
                BlockedReason.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
            reading, exit, unchecked = None, None, ()
        record.update(
            {
                "state": "done",
                "ended_at": now(),
                "outcome": outcome.to_json(),
                "measurements": _measurements(reading, record.get("baseline")),
                "unchecked_budgets": list(unchecked),
                "findings": _findings(reading),
                "obligations": metadata.get("obligations", _obligations(reading)),
                "verification": metadata,
                "exit_code": None if exit is None else exit.code,
                "argv": None
                if exit is None or check is None
                else metadata["argv"] if "authority" in job else list(
                    dispatch.argv_for(
                        check, run_dir, None, input_digest=record["inputs"]["digest"]
                    )
                ),
            }
        )
        seal(record, run_dir, check)
        store.publish(record)
    return record


def _execute_formal(
    job: dict[str, Any], run_id: str, run_dir: Path, store: Store
) -> tuple[Outcome, dict[str, Any]]:
    request = VerificationRequest.from_json(job["request"])
    component = FormalComponent.from_json(job["component"])
    if (
        request.component != component.id
        or component.digest != job["policy_sha256"]
        or component.package.sha256 != job["package_sha256"]
    ):
        return Blocked(
            BlockedReason.NOT_APPROVED, "formal job policy or checker identity changed"
        ), {}
    if isinstance(request.candidate, GitCommit):
        if (
            job["source"]["kind"] != "git_commit"
            or job["source"]["commit"] != request.candidate.oid
        ):
            return Blocked(
                BlockedReason.SOURCE_CHANGED,
                "captured Git commit differs from the request",
            ), {}
    elif job["source"]["kind"] != "working_tree":
        return Blocked(
            BlockedReason.SOURCE_CHANGED,
            "captured source kind differs from the request",
        ), {}
    if component.entry not in {item["path"] for item in job["source"]["files"]}:
        return Blocked(
            BlockedReason.INPUT_MISSING,
            f"formal entry file is missing: {component.entry}",
        ), {}
    approval = store.approved().get(component.digest, {})
    if "authority" not in job and (
        approval.get("kind") != "formal_policy"
        or approval.get("policy_sha256") != component.digest
        or approval.get("package_sha256") != component.package.sha256
        or approval.get("checker_sha256") != job["checker_sha256"]
    ):
        return Blocked(
            BlockedReason.NOT_APPROVED,
            "formal policy, package, and checker are not accepted",
        ), {}
    if store.cancel_requested(run_id):
        return Blocked(BlockedReason.CANCELLED, "stopped on request"), {}
    from .formal.ledger import verify_package
    from .formal.verus import execute_formal

    try:
        verify_package(component)
    except (OSError, ValueError) as exc:
        return Blocked(BlockedReason.NOT_APPROVED, str(exc)), {}
    return execute_formal(
        component,
        run_dir / "snapshot",
        run_dir,
        job["checker_sha256"],
        lambda: store.cancel_requested(run_id),
    )


def _external_job(job: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job["job_id"],
        "run_id": record["run_id"],
        "check_id": job["subject"]["id"],
        "check_digest": job["check_digest"],
        "entry": job["definition"],
        "backend": job["backend"],
        "inputs": job["inputs"],
    }


def run_check(
    project: Project,
    manifest: Manifest,
    check_id: str,
    *,
    store: Store | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Synchronous library entry point using the same job and receipt contract."""
    store = store or Store.of(project)
    record = prepare(project, manifest, check_id, store=store, run_id=run_id)
    return execute(project, record["run_id"], store=store)


def _execute(
    project: Project | None,
    check: CheckSpec,
    store: Store,
    record: dict[str, Any],
    resolved_job: dict[str, Any],
) -> tuple[Outcome, Any, proc.Exit | None, dict[str, Any]]:
    run_id, digest = record["run_id"], record["check_digest"]
    if store.cancel_requested(run_id):
        return Blocked(BlockedReason.CANCELLED, "stopped on request"), None, None, {}
    if "authority" in resolved_job:
        from .ci import execute_empirical

        if record["inputs"]["unmatched"]:
            return Blocked(BlockedReason.INPUT_MISSING, "target policy inputs are missing"), None, None, {}
        return execute_empirical(check, store, record, resolved_job)
    approval = store.approved().get(digest, {})
    if (
        approval.get("kind") != "check_definition"
        or approval.get("backend") != record["backend"]
    ):
        return (
            Blocked(
                BlockedReason.NOT_APPROVED,
                "this check and checker identity have not been accepted; run `vkit accept`",
            ),
            None,
            None,
            {},
        )
    if backend_identity(check) != record["backend"]:
        return (
            Blocked(
                BlockedReason.NOT_APPROVED, "checker identity changed after submission"
            ),
            None,
            None,
            {},
        )
    if record["inputs"]["unmatched"]:
        return (
            Blocked(
                BlockedReason.INPUT_MISSING,
                "declared inputs match no file: "
                + ", ".join(record["inputs"]["unmatched"]),
            ),
            None,
            None,
            {},
        )
    blocked = missing_prerequisite(check)
    if blocked is not None:
        return blocked, None, None, {}
    run_dir = store.run_dir(run_id)
    metadata = {}
    if check.kind.value == "external":
        from .verifiers import external

        execution_job = _external_job(resolved_job, record)
        if (run_dir / "replay").is_dir():
            outcome, metadata = external.replay(
                check,
                run_dir / "replay",
                run_dir,
                execution_job,
                lambda: store.cancel_requested(run_id),
            )
        else:
            outcome, metadata = external.execute(
                check, run_dir, execution_job, lambda: store.cancel_requested(run_id)
            )
        reading, exit = None, None
    else:
        exit = proc.run(
            dispatch.argv_for(
                check, run_dir, None, input_digest=record["inputs"]["digest"]
            ),
            cwd=check.cwd,
            env=_child_env(),
            stdout_path=run_dir / "stdout.log",
            stderr_path=run_dir / "stderr.log",
            timeout_seconds=check.timeout_seconds,
            should_cancel=lambda: store.cancel_requested(run_id),
        )
        outcome, reading = _derive(
            check, exit, run_dir / check.artifact_name, record["baseline"]
        )
    _record_logical_trust(check, reading, metadata, run_dir, record)
    after = Snapshot(project).inputs(check.inputs)
    current_source = capture_working_tree(
        project, [item["path"] for item in record["source"]["files"]]
    )
    changed = [
        item["path"]
        for item in record["inputs"]["files"]
        if not (run_dir / "snapshot" / item["path"]).is_file()
        or file_digest(run_dir / "snapshot" / item["path"]) != item["sha256"]
    ]
    if (
        after.digest != record["inputs"]["digest"]
        or current_source.digest != record["source"]["digest"]
        or changed
    ):
        return (
            Blocked(
                BlockedReason.SOURCE_CHANGED,
                "inputs changed while the check ran: " + ", ".join(changed[:10]),
            ),
            None,
            exit,
            {},
        )
    if backend_identity(check) != record["backend"]:
        return (
            Blocked(
                BlockedReason.NOT_APPROVED, "checker identity changed during execution"
            ),
            None,
            exit,
            {},
        )
    return outcome, reading, exit, metadata
