"""Run one registered check and record what it established."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import proc
from .inputs import Snapshot
from .manifest import Manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome
from .paths import Project
from .state import evidence_key
from .store import Store, new_run_id, now
from .verifiers import dispatch
from .verifiers.spec import CheckSpec

_VKIT_SRC = str(Path(__file__).resolve().parents[1])


def missing_prerequisite(check: CheckSpec) -> Blocked | None:
    for need in check.prerequisites:
        if shutil.which(need.executable) is None:
            return Blocked(BlockedReason.PREREQUISITE_MISSING, f"{need.name}: {need.executable!r} is not on PATH")
    return None


def _child_env() -> dict[str, str]:
    previous = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": _VKIT_SRC if not previous else os.pathsep.join([_VKIT_SRC, previous])}


def _derive(check: CheckSpec, exit: proc.Exit, artifact: Path) -> tuple[Outcome, Any]:
    if exit.launch_error is not None:
        return Blocked(BlockedReason.LAUNCH_FAILED, exit.launch_error), None
    if exit.cancelled:
        return Blocked(BlockedReason.CANCELLED, "stopped on request; descendants were stopped"), None
    if exit.timed_out:
        return Blocked(BlockedReason.TIMEOUT, f"exceeded {check.timeout_seconds:g}s; descendants were stopped"), None
    if not artifact.is_file():
        return Blocked(BlockedReason.ARTIFACT_MISSING, f"{check.artifact_name} was not written"), None
    reading = dispatch.interpret(check, artifact.read_bytes())
    outcome = dispatch.outcome_from_reading(reading)
    if isinstance(outcome, Blocked):
        return outcome, None
    if isinstance(outcome, Failed):
        return outcome, reading
    if exit.code not in (0, 1):
        return Blocked(BlockedReason.ARTIFACT_MALFORMED,
                       f"the check exited {exit.code} after writing an artifact, so the artifact is not trusted"), None
    return outcome, reading


def _obligations(reading: Any) -> dict[str, Any] | None:
    if reading is None or not hasattr(reading, "obligation_results"):
        return None
    satisfied, counterexamples = reading.obligation_results()
    return {"satisfied": list(satisfied), "counterexamples": list(counterexamples)}


def run_check(project: Project, manifest: Manifest, check_id: str, *,
              store: Store | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Run `check_id` in the foreground and return its published record."""
    store = store or Store.of(project)
    check = manifest.require(check_id)
    run_id = run_id or new_run_id()
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    digest = manifest.digest(check_id)
    before = Snapshot(project).inputs(check.inputs)
    record: dict[str, Any] = {
        "run_id": run_id, "check_id": check_id, "check_digest": digest,
        "key": evidence_key(digest, before), "inputs": before.to_json(),
        "category": check.evidence_kind().value, "worktree": str(project.root),
        "state": "running", "started_at": now(), "ended_at": None,
        "outcome": None, "obligations": None, "measurements": [], "argv": None, "exit_code": None,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
    }
    with store.hold(run_id):
        store.save(record)
        outcome, reading, exit = _execute(project, manifest, check, store, run_id, digest, before)
        record.update({
            "state": "done", "ended_at": now(), "outcome": outcome.to_json(),
            "obligations": _obligations(reading),
            "exit_code": None if exit is None else exit.code,
            "argv": None if exit is None else list(dispatch.argv_for(check, run_dir, None)),
        })
        store.publish(record)
    return record


def _execute(project: Project, manifest: Manifest, check: CheckSpec, store: Store, run_id: str,
             digest: str, before) -> tuple[Outcome, Any, proc.Exit | None]:
    if digest not in store.approved():
        return Blocked(BlockedReason.NOT_APPROVED,
                       "this check's definition has not been accepted; run `vkit accept` to review it"), None, None
    if before.unmatched:
        return Blocked(BlockedReason.INPUT_MISSING,
                       "declared inputs match no file: " + ", ".join(before.unmatched)), None, None
    blocked = missing_prerequisite(check)
    if blocked is not None:
        return blocked, None, None
    run_dir = store.run_dir(run_id)
    exit = proc.run(
        dispatch.argv_for(check, run_dir, None), cwd=check.cwd, env=_child_env(),
        stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log",
        timeout_seconds=check.timeout_seconds,
        should_cancel=lambda: store.cancel_requested(run_id),
    )
    outcome, reading = _derive(check, exit, run_dir / check.artifact_name)
    after = Snapshot(project).inputs(check.inputs)
    if after.digest != before.digest:
        changed = sorted({p for p, _ in set(before.files) ^ set(after.files)})
        return Blocked(BlockedReason.SOURCE_CHANGED,
                       "inputs changed while the check ran: " + ", ".join(changed[:10])), None, exit
    return outcome, reading, exit
