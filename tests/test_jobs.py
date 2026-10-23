from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from helpers import DRIVER, NO_WINDOW, git, scenario_check, vkit
from test_external import _project
from vkit.paths import open_project
from vkit.store import Store, write_json


def test_queued_attempt_reports_a_dead_worker_as_interrupted(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "queued", "state": "queued"})
    worker_path = store.run_dir("queued") / "worker.json"
    write_json(worker_path, {"pid": os.getpid()})
    assert store.record("queued")["state"] == "queued"
    worker = subprocess.Popen([sys.executable, "-c", "pass"], **NO_WINDOW)
    worker.wait(timeout=10)
    write_json(worker_path, {"pid": worker.pid})
    assert store.record("queued")["state"] == "interrupted"


def _passed(make_project):
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n'},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    code, body = vkit(project, "check", "run", "--check", "ok")
    assert code == 0, body
    return project, body["runs"][0]


@pytest.mark.parametrize("target", ["artifact", "receipt", "snapshot", "record"])
def test_gate_rejects_tampered_retained_evidence(make_project, target):
    project, record = _passed(make_project)
    assert vkit(project, "gate")[0] == 0
    directory = project / ".git/vkit/runs" / record["run_id"]
    path = directory / {"artifact": "result.json", "receipt": "receipt.json",
                        "snapshot": "snapshot/ok.py", "record": "record.json"}[target]
    if target == "record":
        value = json.loads(path.read_text())
        value["backend"] = {"forged": True}
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_text("corrupted", encoding="utf-8")
    assert vkit(project, "gate")[0] == 3
    code, body = vkit(project, "evidence", "export", "--run", record["run_id"], "--output", str(project / "bundle"))
    assert code == 2 and "intact" in body["error"]


def test_detached_attempts_are_immutable_and_retry_keeps_job_identity(make_project):
    project, first = _passed(make_project)
    code, second = vkit(project, "run", "retry", "--run", first["run_id"], "--detach")
    assert code == 0 and second["run_id"] != first["run_id"]
    assert second["job_id"] == first["job_id"]
    code, result = vkit(project, "run", "wait", "--run", second["run_id"], "--timeout", "30")
    assert (code, result["outcome"]["result"]) == (0, "PASS")
    code, old = vkit(project, "run", "show", "--run", first["run_id"])
    assert (code, old["outcome"], old["receipt"]) == (0, first["outcome"], first["receipt"])


def test_an_attempt_cannot_be_executed_twice(make_project):
    project, record = _passed(make_project)
    store = Store.of(open_project(project))
    with store.hold(record["run_id"]):
        result = subprocess.run([sys.executable, "-m", "vkit.jobs", "--project", str(project),
                                 "--run", record["run_id"]], capture_output=True, text=True, **NO_WINDOW)
    assert result.returncode != 0 and "already held" in result.stderr
    assert store.record(record["run_id"])["receipt"] == record["receipt"]
    assert vkit(project, "gate")[0] == 0


def test_duplicate_worker_cannot_overwrite_a_live_attempt(make_project):
    project = make_project({"slow.py": DRIVER + 'import time; time.sleep(30)\nreport([("ok", True, "ok")])\n'},
                           [scenario_check("slow", "slow.py", scenarios=["ok"], inputs=["slow.py"])])
    _, submitted = vkit(project, "check", "run", "--check", "slow", "--detach")
    run_id = submitted["runs"][0]["run_id"]
    store = Store.of(open_project(project))
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            record = store.record(run_id)
            if record["state"] == "running":
                break
            time.sleep(0.05)
        assert record["state"] == "running"
        duplicate = subprocess.run([sys.executable, "-m", "vkit.jobs", "--project", str(project), "--run", run_id],
                                   capture_output=True, text=True, check=False, **NO_WINDOW)
        assert duplicate.returncode != 0 and "already held" in duplicate.stderr
        assert store.record(run_id)["state"] == "running"
    finally:
        store.request_cancel(run_id)
        code, stopped = vkit(project, "run", "wait", "--run", run_id, "--timeout", "30")
    assert (code, stopped["outcome"]["reason"]) == (3, "cancelled")


def test_external_jobs_export_and_replay_with_a_fresh_checker_receipt(tmp_path):
    root, raw, _, _, producer, checker = _project(tmp_path)
    (root / "verification").mkdir()
    (root / "verification/manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    git(root, "init", "-q")
    assert vkit(root, "accept", "--yes")[0] == 0
    code, body = vkit(root, "check", "run", "--check", "formal")
    assert code == 0, body
    original = body["runs"][0]
    assert original["receipt"]["assurance"] == "kernel_proof"
    bundle = tmp_path / "bundle"
    assert vkit(root, "evidence", "export", "--run", original["run_id"], "--output", str(bundle))[0] == 0
    code, replayed = vkit(root, "evidence", "replay", "--bundle", str(bundle))
    assert (code, replayed["outcome"]["result"]) == (0, "PASS"), replayed
    assert replayed["run_id"] != original["run_id"] and replayed["job_id"] == original["job_id"]
    replay_dir = root / ".git/vkit/runs" / replayed["run_id"]
    assert not (replay_dir / "producer-request.json").exists()
    receipt = json.loads((replay_dir / "checker-receipt.json").read_text())
    assert receipt["run_id"] == replayed["run_id"]
    (bundle / "evidence/proof.json").write_text("tampered", encoding="utf-8")
    assert vkit(root, "evidence", "replay", "--bundle", str(bundle))[0] == 2
    checker.write_text(checker.read_text() + "\n# changed trusted code\n", encoding="utf-8")
    assert vkit(root, "gate")[0] == 3
    assert vkit(root, "evidence", "replay", "--bundle", str(bundle))[0] == 2
