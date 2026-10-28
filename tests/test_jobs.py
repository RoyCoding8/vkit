from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from helpers import DRIVER, NO_WINDOW, git, scenario_check, vkit
from test_external import _project
from vkit.paths import open_project
from vkit.formal.policy import FormalComponent, GitCommit, LedgerBindings, PackageIdentity, VerificationRequest
from vkit.outcome import Passed
from vkit.runner import execute, prepare_formal
from vkit.inputs import canonical_digest
from vkit.store import Store, write_json


def test_wait_does_not_finish_before_the_worker_releases_its_attempt(tmp_path):
    from vkit.jobs import wait

    store = Store(tmp_path / "state")
    store.save({"run_id": "published", "state": "done"})
    finished = threading.Event()
    result = []

    def client():
        result.append(wait(store, "published", timeout_seconds=2))
        finished.set()

    with store.hold("published"):
        thread = threading.Thread(target=client)
        thread.start()
        assert not finished.wait(0.1)
    thread.join(timeout=3)
    assert finished.is_set()
    assert result == [{"run_id": "published", "state": "done"}]


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


@pytest.mark.parametrize("target", ["artifact", "receipt", "snapshot", "record", "unlisted"])
def test_gate_rejects_tampered_retained_evidence(make_project, target):
    project, record = _passed(make_project)
    assert vkit(project, "gate")[0] == 0
    directory = project / ".git/vkit/runs" / record["run_id"]
    path = directory / {"artifact": "result.json", "receipt": "receipt.json",
                        "snapshot": "snapshot/ok.py", "record": "record.json", "unlisted": "unlisted.txt"}[target]
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
    assert (code, result["summary"]["outcome"]["result"]) == (0, "PASS")
    code, old = vkit(project, "run", "show", "--run", first["run_id"])
    assert (code, old["summary"]["outcome"], old["summary"]["receipt"]) == (
        0, {"result": "PASS"}, first["receipt"],
    )


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
    assert (code, stopped["summary"]["outcome"]["reason"]) == (3, "cancelled")


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


def test_formal_job_executes_from_captured_commit_without_opening_checkout(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "source.rs").write_bytes(b"pub fn reserve() {}\r\n")
    git(root, "add", "source.rs")
    git(root, "commit", "-qm", "initial")
    project = open_project(root)
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, **NO_WINDOW
    ).stdout.strip()
    component = FormalComponent(
        "ledger",
        PackageIdentity("ledger-core", "1.0.0", "a" * 64),
        "reserve safety",
        "source.rs",
        "strict",
        4,
        LedgerBindings("Ledger::new", "Ledger::reserve", "Ledger::release", "Ledger::reserve_batch", "Ledger::apply_command", "Ledger::run_commands"),
        True,
    )
    request = VerificationRequest("ledger", GitCommit(oid))
    identity = {"semantic": {"profile": "strict", "image_digest": "b" * 64}, "paths": {"verus_root": "C:/verus"}}
    store = Store.of(project)
    checker = canonical_digest(identity["semantic"])
    record = prepare_formal(project, request, component, checker, store=store)
    job = json.loads((store.run_dir(record["run_id"]) / "job.json").read_text(encoding="utf-8"))
    assert job["source"]["commit"] == oid
    assert job["source"]["files"][0]["raw_sha256"]
    assert job["checker_sha256"] == checker
    store.approve({component.digest: {"kind": "formal_policy", "policy_sha256": component.digest,
                                    "package_sha256": component.package.sha256,
                                    "checker_sha256": job["checker_sha256"], "accepted_at": "now"}})
    moved = tmp_path / "moved-repo"
    root.rename(moved)
    store = Store(moved / ".git" / "vkit")
    assert not root.exists()
    ledger = types.ModuleType("vkit.formal.ledger")
    def verify_package(_):
        return None
    ledger.verify_package = verify_package
    verus = types.ModuleType("vkit.formal.verus")
    def execute_formal(actual_component, snapshot_root, run_dir, actual_checker, should_cancel):
        assert actual_component == component
        assert (snapshot_root / "source.rs").read_bytes() == b"pub fn reserve() {}\r\n"
        assert actual_checker == checker and not should_cancel()
        return Passed(()), {"obligations": {"satisfied": ["ledger:reserve"], "counterexamples": []},
                            "execution_identity": {"semantic": identity["semantic"], "digest": checker}}
    verus.execute_formal = execute_formal
    monkeypatch.setitem(sys.modules, "vkit.formal.ledger", ledger)
    monkeypatch.setitem(sys.modules, "vkit.formal.verus", verus)
    result = execute(None, record["run_id"], store=store)
    assert result["outcome"]["result"] == "PASS"
    assert result["subject"] == {"kind": "formal_goal", "id": "ledger"}
    assert (store.root / "evidence" / "formal_goal" / "ledger" / f"{job['job_id']}.json").is_file()
    from dataclasses import replace

    project = open_project(moved)
    reusable = replace(request, reuse=True)
    reused = prepare_formal(project, reusable, component, checker, store=store)
    assert reused["run_id"] == record["run_id"]
    accepted = store.approved()[component.digest]
    for field in ("policy_sha256", "package_sha256", "checker_sha256"):
        store.approve({component.digest: {**accepted, field: "e" * 64}})
        rejected_reuse = prepare_formal(project, reusable, component, checker, store=store)
        assert rejected_reuse["run_id"] != record["run_id"] and rejected_reuse["state"] == "queued"
    store.approve({component.digest: accepted})
    for changed_component, changed_checker in ((replace(component, max_records=5), checker),
                                                (component, "d" * 64)):
        fresh = prepare_formal(project, reusable, changed_component, changed_checker, store=store)
        assert fresh["run_id"] != record["run_id"] and fresh["state"] == "queued"
    (moved / "dependency.rs").write_text("pub const LIMIT: u32 = 9;\n", encoding="utf-8")
    git(moved, "add", "dependency.rs")
    git(moved, "commit", "-qm", "change captured dependency")
    from vkit.formal.service import resolve_commit

    changed = replace(reusable, candidate=GitCommit(resolve_commit(project, "HEAD")))
    assert prepare_formal(project, changed, component, checker, store=store)["run_id"] != record["run_id"]
    (store.run_dir(record["run_id"]) / "receipt.json").write_text("{}", encoding="utf-8")
    assert prepare_formal(project, reusable, component, checker, store=store)["run_id"] != record["run_id"]
