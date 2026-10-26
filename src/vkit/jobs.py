"""Durable local jobs. CLI, MCP, and CI share this process-independent service."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .evidence import backend_identity, validate_record
from .inputs import Snapshot
from .manifest import Manifest, parse_manifest_data
from .nowindow import hidden_window
from .outcome import Blocked, BlockedReason
from .paths import Project, open_project
from .runner import _child_env, execute, prepare
from .store import Store, now, read_json, write_json


def _failed_submission(store: Store, run_id: str, outcome: Blocked) -> None:
    try:
        with store.hold(run_id):
            record = read_json(store.run_dir(run_id) / "record.json")
            if record is not None and record["state"] == "queued":
                record.update(state="done", ended_at=now(), outcome=outcome.to_json())
                store.save(record)
    except RuntimeError:
        # A live worker owns publication; a failed competing launcher cannot overwrite it.
        return


def _launch(project: Project, store: Store, record: dict[str, Any]) -> dict[str, Any]:
    run_id = record["run_id"]
    options = hidden_window()
    if os.name == "nt":
        options["creationflags"] |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        with (store.run_dir(run_id) / "worker.log").open("ab") as log:
            worker = subprocess.Popen([sys.executable, "-m", "vkit.jobs", "--project", str(project.root),
                              "--run", run_id], cwd=project.root, env=_child_env(),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **options)
            write_json(store.run_dir(run_id) / "worker.json", {"pid": worker.pid})
    except OSError as exc:
        # Submission failure is durable and visible; it never publishes passing evidence.
        _failed_submission(store, run_id, Blocked(BlockedReason.LAUNCH_FAILED, str(exc)))
        record = store.record(run_id)
    return record


def submit(project: Project, manifest: Manifest, check_id: str, *, store: Store | None = None) -> dict[str, Any]:
    store = store or Store.of(project)
    return _launch(project, store, prepare(project, manifest, check_id, store=store))


def wait(store: Store, run_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
    if timeout_seconds is not None and timeout_seconds < 0:
        raise ValueError("timeout_seconds must be nonnegative")
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        record = store.record(run_id)
        if record is None:
            raise ValueError(f"no run {run_id!r}")
        pending = (record["state"] in ("queued", "running")
                   or (record["state"] == "done" and store.is_alive(run_id)))
        if not pending or (deadline is not None and time.monotonic() >= deadline):
            return record
        time.sleep(0.05)


def replay(project: Project, manifest: Manifest, bundle: Path, *, store: Store | None = None) -> dict[str, Any]:
    """Recheck evidence with the current accepted checker, never commands from a bundle."""
    store = store or Store.of(project)
    bundle = bundle.resolve()
    source = read_json(bundle / "record.json")
    if not isinstance(source, dict) or source.get("state") != "done" or not validate_record(source, bundle):
        raise ValueError("bundle is incomplete or its evidence digests do not match")
    check = manifest.require(source["check_id"])
    if (manifest.digest(check.id) != source["check_digest"]
            or Snapshot(project).inputs(check.inputs).digest != source["inputs"]["digest"]
            or backend_identity(check) != source["backend"]):
        raise ValueError("bundle does not match the current specification, inputs, and checker identity")
    if check.kind.value not in ("external", "lean", "tlc"):
        raise ValueError("evidence replay requires an external, Lean, or TLC checker")
    record = prepare(project, manifest, check.id, store=store, replay_source=bundle,
                     replay_receipt=source["receipt"]["sha256"])
    return _launch(project, store, record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    project = open_project(args.project)
    store = Store.of(project)
    try:
        job = read_json(store.run_dir(args.run) / "job.json")
        manifest = parse_manifest_data({"schema_version": 3, "checks": [job["entry"]]},
                                       project=project, origin="persisted job")
        execute(project, manifest, args.run, store=store)
    except Exception as exc:
        _failed_submission(store, args.run, Blocked(
            BlockedReason.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"))
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
