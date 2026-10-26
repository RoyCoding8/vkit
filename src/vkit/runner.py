"""Run one registered check and record what it established."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import proc
from .budgets import evaluate
from .evidence import backend_identity, freeze, seal
from .inputs import Snapshot, file_digest
from .manifest import Manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed
from .paths import Project
from .state import evidence_key
from .store import Store, new_run_id, now, write_json
from .verifiers import dispatch
from .verifiers import static_adapter
from .verifiers.dispatch import ScenarioReading
from .verifiers.spec import CheckSpec

_VKIT_SRC = str(Path(__file__).resolve().parents[1])


def missing_prerequisite(check: CheckSpec) -> Blocked | None:
    for need in check.prerequisites:
        if shutil.which(need.executable) is None:
            return Blocked(BlockedReason.PREREQUISITE_MISSING, f"{need.name}: {need.executable!r} is not on PATH")
    return None


def _child_env() -> dict[str, str]:
    previous = os.environ.get("PYTHONPATH")
    paths = dict.fromkeys([_VKIT_SRC, *(previous.split(os.pathsep) if previous else [])])
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


def _derive(check: CheckSpec, exit: proc.Exit, artifact: Path, baseline: dict[str, Any] | None) -> tuple[Outcome, Any]:
    if exit.launch_error is not None:
        return Blocked(BlockedReason.LAUNCH_FAILED, exit.launch_error), None
    if exit.cancelled:
        return Blocked(BlockedReason.CANCELLED, "stopped on request; descendants were stopped"), None
    if exit.timed_out:
        return Blocked(BlockedReason.TIMEOUT, f"exceeded {check.timeout_seconds:g}s; descendants were stopped"), None
    if not artifact.is_file() or artifact.is_symlink():
        return Blocked(BlockedReason.ARTIFACT_MISSING, f"{check.artifact_name} was not written"), None
    reading = dispatch.interpret(check, artifact.read_bytes())
    outcome = dispatch.outcome_from_reading(reading, baseline)
    if isinstance(outcome, Blocked):
        return outcome, None
    if isinstance(outcome, Failed):
        return outcome, reading
    if exit.code not in (0, 1) or (check.kind.value in ("lean", "tlc") and exit.code != 0):
        return Blocked(BlockedReason.ARTIFACT_MALFORMED,
                       f"the check exited {exit.code} after writing an artifact, so the artifact is not trusted"), None
    return outcome, reading


def _apply_budgets(check: CheckSpec, outcome: Outcome, reading: Any,
                   baseline: dict[str, Any] | None) -> tuple[Outcome, tuple[str, ...]]:
    if not isinstance(reading, ScenarioReading) or isinstance(outcome, Blocked):
        return outcome, ()
    results, unchecked = evaluate(getattr(check, "budgets", ()), reading.measurements,
                                  (baseline or {}).get("measurements"))
    if not results:
        return outcome, unchecked
    scenarios = outcome.scenarios + results
    return (Failed(scenarios) if any(not r.passed for r in scenarios) else Passed(scenarios)), unchecked


def _measurements(reading: Any, baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(reading, ScenarioReading):
        return []
    pinned = (baseline or {}).get("measurements", {})
    return [{**m.to_json(), "baseline": pinned.get(m.name)} for m in reading.measurements]


def _findings(reading: Any) -> list[dict[str, Any]]:
    return [f.to_json() for f in reading.findings] if isinstance(reading, static_adapter.StaticReading) else []


def _obligations(reading: Any) -> dict[str, Any] | None:
    if reading is None or not hasattr(reading, "obligation_results"):
        return None
    satisfied, counterexamples = reading.obligation_results()
    return {"satisfied": list(satisfied), "counterexamples": list(counterexamples)}


def prepare(project: Project, manifest: Manifest, check_id: str, *,
            store: Store, run_id: str | None = None, replay_source: Path | None = None,
            replay_receipt: str | None = None) -> dict[str, Any]:
    """Persist an immutable job and a separate queued execution attempt."""
    check = manifest.require(check_id)
    run_id = run_id or new_run_id()
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    digest = manifest.digest(check_id)
    baseline = store.baselines().get(check_id)
    before = Snapshot(project).inputs(check.inputs)
    backend = backend_identity(check)
    key = evidence_key(digest, before, baseline, backend)
    record: dict[str, Any] = {
        "schema_version": 1, "job_id": key, "backend": backend,
        "run_id": run_id, "check_id": check_id, "check_digest": digest,
        "key": key, "inputs": before.to_json(), "baseline": baseline,
        "category": check.evidence_kind().value, "worktree": str(project.root),
        "state": "queued", "submitted_at": now(), "started_at": None, "ended_at": None,
        "outcome": None, "obligations": None, "measurements": [], "argv": None, "exit_code": None,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
    }
    freeze(project.root, record["inputs"], run_dir / "snapshot")
    job = {**{k: record[k] for k in ("schema_version", "job_id", "run_id", "check_id", "check_digest", "inputs", "backend")},
           "entry": manifest.entries[check_id], "worktree": str(project.root)}
    if replay_source is not None:
        from .evidence import validate_record
        from .store import read_json

        retained = run_dir / "replay"
        source_record = read_json(replay_source / "record.json")
        if source_record["receipt"]["sha256"] != replay_receipt:
            raise ValueError("source receipt changed before replay capture")
        shutil.copytree(replay_source, retained, ignore=shutil.ignore_patterns("lock", "cancel", "worker.log", "worker.json", "replay"))
        if not validate_record(source_record, retained):
            raise ValueError("evidence changed while capturing replay bundle")
        job["replay_source"] = str(retained)
        record["replayed_from"] = {"run_id": source_record["run_id"], "receipt": source_record["receipt"]["sha256"]}
    write_json(run_dir / "job.json", job)
    store.save(record)
    return record


def execute(project: Project, manifest: Manifest, run_id: str, *, store: Store) -> dict[str, Any]:
    """Claim one attempt. The OS lock prevents two workers from executing it."""
    check_id = store.record(run_id)["check_id"]
    check = manifest.require(check_id)
    run_dir = store.run_dir(run_id)
    with store.hold(run_id):
        record = store.record(run_id)
        if record["state"] != "queued":
            return record
        record.update(state="running", started_at=now(), worker_pid=os.getpid())
        store.save(record)
        frozen = replace(check, cwd=run_dir / "snapshot" / check.cwd.relative_to(project.root))
        frozen.cwd.mkdir(parents=True, exist_ok=True)
        metadata: dict[str, Any] = {}
        try:
            if manifest.digest(check_id) != record["check_digest"]:
                outcome, reading, exit = Blocked(BlockedReason.NOT_APPROVED, "job definition does not match its accepted digest"), None, None
            else:
                outcome, reading, exit, metadata = _execute(project, check, frozen, store, record)
            outcome, unchecked = _apply_budgets(check, outcome, reading, record["baseline"])
        except Exception as exc:
            outcome = Blocked(BlockedReason.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            reading, exit, unchecked = None, None, ()
        record.update({
            "state": "done", "ended_at": now(), "outcome": outcome.to_json(),
            "measurements": _measurements(reading, record["baseline"]), "unchecked_budgets": list(unchecked),
            "findings": _findings(reading),
            "obligations": metadata.get("obligations", _obligations(reading)),
            "verification": metadata,
            "exit_code": None if exit is None else exit.code,
            "argv": None if exit is None else list(dispatch.argv_for(frozen, run_dir, None, input_digest=record["inputs"]["digest"])),
        })
        seal(record, run_dir, check)
        store.publish(record)
    return record


def run_check(project: Project, manifest: Manifest, check_id: str, *,
              store: Store | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Synchronous library entry point using the same job and receipt contract."""
    store = store or Store.of(project)
    record = prepare(project, manifest, check_id, store=store, run_id=run_id)
    return execute(project, manifest, record["run_id"], store=store)


def _execute(project: Project, check: CheckSpec, frozen: CheckSpec, store: Store,
             record: dict[str, Any]) -> tuple[Outcome, Any, proc.Exit | None, dict[str, Any]]:
    run_id, digest = record["run_id"], record["check_digest"]
    if store.cancel_requested(run_id):
        return Blocked(BlockedReason.CANCELLED, "stopped on request"), None, None, {}
    approval = store.approved().get(digest, {})
    if approval.get("kind") != "check_definition" or approval.get("backend") != record["backend"]:
        return Blocked(BlockedReason.NOT_APPROVED,
                       "this check and checker identity have not been accepted; run `vkit accept`"), None, None, {}
    if backend_identity(check) != record["backend"]:
        return Blocked(BlockedReason.NOT_APPROVED, "checker identity changed after submission"), None, None, {}
    if record["inputs"]["unmatched"]:
        return Blocked(BlockedReason.INPUT_MISSING,
                       "declared inputs match no file: " + ", ".join(record["inputs"]["unmatched"])), None, None, {}
    blocked = missing_prerequisite(check)
    if blocked is not None:
        return blocked, None, None, {}
    run_dir = store.run_dir(run_id)
    metadata = {}
    if check.kind.value == "external":
        from .verifiers import external
        from .store import read_json

        job = read_json(run_dir / "job.json")
        if "replay_source" in job:
            outcome, metadata = external.replay(frozen, Path(job["replay_source"]), run_dir, job,
                                                lambda: store.cancel_requested(run_id))
        else:
            outcome, metadata = external.execute(frozen, run_dir, job, lambda: store.cancel_requested(run_id))
        reading, exit = None, None
    else:
        exit = proc.run(
            dispatch.argv_for(frozen, run_dir, None, input_digest=record["inputs"]["digest"]), cwd=frozen.cwd, env=_child_env(),
            stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log",
            timeout_seconds=check.timeout_seconds,
            should_cancel=lambda: store.cancel_requested(run_id),
        )
        outcome, reading = _derive(frozen, exit, run_dir / check.artifact_name, record["baseline"])
    after = Snapshot(project).inputs(check.inputs)
    changed = [item["path"] for item in record["inputs"]["files"]
               if not (run_dir / "snapshot" / item["path"]).is_file()
               or file_digest(run_dir / "snapshot" / item["path"]) != item["sha256"]]
    if after.digest != record["inputs"]["digest"] or changed:
        return Blocked(BlockedReason.SOURCE_CHANGED,
                       "inputs changed while the check ran: " + ", ".join(changed[:10])), None, exit, {}
    if backend_identity(check) != record["backend"]:
        return Blocked(BlockedReason.NOT_APPROVED, "checker identity changed during execution"), None, exit, {}
    return outcome, reading, exit, metadata
