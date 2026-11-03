from __future__ import annotations

from copy import deepcopy

import pytest

from helpers import DRIVER, scenario_check, vkit
from test_paired import _repository
from vkit import jobs, query, runner
from vkit.evidence import sha256, validate_record
from vkit.manifest import parse_manifest
from vkit.paths import ProjectError, open_project
from vkit.runner import execute, prepare_change, prepare_retry
from vkit.schemas import EVIDENCE_RECORD, SchemaValidationError, validate
from vkit.store import Store, read_json, write_json


def _queued(tmp_path, *, delta=0):
    root, target, request = _repository(
        tmp_path, language="python", candidate_delta=delta,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    project = open_project(root)
    store = Store.of(project)
    record = prepare_change(
        project, request, trusted_target_oid=target,
        requested_method="finite_paired", store=store,
    )
    return project, store, record


@pytest.mark.parametrize("delta,result,status", [
    (0, "PASS", "observed_conformance"), (2, "FAIL", "observed_violation"),
])
def test_shadow_job_executes_captures_and_seals_scoped_receipt(tmp_path, delta, result, status):
    project, store, queued = _queued(tmp_path, delta=delta)
    (project.root / "candidate.py").write_text("raise RuntimeError('live source must not run')\n")

    record = execute(project, queued["run_id"], store=store)

    assert record["state"] == "done"
    assert record["outcome"]["result"] == result, record["outcome"]
    report = record["verification"]["change_certification"]
    assert report["status"] == status
    assert report["requirement"]["source"] == "requirements.md#conversion"
    assert [(r["method"], r["status"]) for r in report["methods"]] == [("finite_paired", status)]
    assert record["subject"] == {"kind": "change_certification", "id": report["intent_sha256"]}
    assert record["assurance"] == "empirical"
    assert record["claim_outcome"] == "unknown"
    assert record["source_proof"] is False and record["approval_authority"] is False
    directory = store.run_dir(record["run_id"])
    assert validate_record(record, directory)
    assert store.evidence_for("change_certification", report["intent_sha256"], record["job_id"])
    assert store.evidence_for("registered_check", report["intent_sha256"], record["job_id"]) is None
    assert store.evidence_for("formal_goal", report["intent_sha256"], record["job_id"]) is None


def test_shadow_retry_preserves_job_plan_and_two_captures(tmp_path, monkeypatch):
    project, store, queued = _queued(tmp_path)
    store.request_cancel(queued["run_id"])
    first = execute(project, queued["run_id"], store=store)
    assert first["outcome"]["result"] == "BLOCKED"
    assert first["verification"]["change_certification"]["stopped"] == "cancelled"
    assert validate_record(first, store.run_dir(first["run_id"]))
    (project.root / "candidate.py").write_text("raise RuntimeError('changed checkout')\n")
    monkeypatch.setattr(runner, "capture_git_commit", lambda *a, **k: pytest.fail("recaptured Git source"))

    retried = prepare_retry(first["run_id"], store=store)
    assert retried["run_id"] != first["run_id"]
    assert retried["job_id"] == first["job_id"]
    assert read_json(store.run_dir(retried["run_id"]) / "job.json") == read_json(
        store.run_dir(first["run_id"]) / "job.json"
    )
    done = execute(project, retried["run_id"], store=store)
    assert done["outcome"]["result"] == "PASS", done["outcome"]
    assert validate_record(done, store.run_dir(done["run_id"]))


@pytest.mark.parametrize("snapshot", ["snapshot", "target-snapshot"])
def test_shadow_worker_rejects_either_changed_capture(tmp_path, snapshot):
    project, store, queued = _queued(tmp_path)
    (store.run_dir(queued["run_id"]) / snapshot / "candidate.py").write_text("def convert(v): return 99\n")
    with pytest.raises(ProjectError, match="captured source bytes"):
        execute(project, queued["run_id"], store=store)
    assert store.record(queued["run_id"])["outcome"] is None


@pytest.mark.parametrize("field", ["method_plan", "bound_request_sha256", "implementation"])
def test_shadow_worker_rejects_record_drift_before_execution(tmp_path, field):
    project, store, queued = _queued(tmp_path)
    queued[field] = {} if field != "bound_request_sha256" else "0" * 64
    store.save(queued)
    with pytest.raises(ValueError, match="change record"):
        execute(project, queued["run_id"], store=store)


def test_shadow_worker_rejects_installation_drift_before_execution(tmp_path, monkeypatch):
    project, store, queued = _queued(tmp_path)
    monkeypatch.setattr(runner, "policy_identity", lambda: {"runner.py": "0" * 64})
    with pytest.raises(ValueError, match="queued identity"):
        execute(project, queued["run_id"], store=store)


def test_shadow_receipt_cannot_be_promoted_even_with_rewritten_receipt_hash(tmp_path):
    project, store, queued = _queued(tmp_path)
    store.request_cancel(queued["run_id"])
    record = execute(project, queued["run_id"], store=store)
    directory = store.run_dir(record["run_id"])
    receipt = read_json(directory / "receipt.json")
    for field, forged in [
        ("claim_outcome", "established"), ("assurance", "trusted_engine"),
        ("method", "verus_source"), ("source_proof", True), ("approval_authority", True),
    ]:
        changed = deepcopy(receipt)
        changed[field] = forged
        write_json(directory / "receipt.json", changed)
        changed_record = deepcopy(record)
        changed_record[field] = forged
        if field in {"claim_outcome", "assurance"}:
            changed_record["receipt"][field] = forged
        changed_record["receipt"]["sha256"] = sha256(directory / "receipt.json")
        assert not validate_record(changed_record, directory), field


def test_shadow_pass_does_not_satisfy_same_named_required_check(tmp_path):
    project, store, queued = _queued(tmp_path)
    subject_id = queued["subject"]["id"]
    check = scenario_check(subject_id, "candidate.py", scenarios=["required"], inputs=["candidate.py"])
    write_json(project.manifest_path, {"schema_version": 3, "description": "isolation", "checks": [check]})
    before = query.status(query.open_context(project.root))["gate"]
    record = execute(project, queued["run_id"], store=store)
    assert record["outcome"]["result"] == "PASS", record["outcome"]
    assert query.status(query.open_context(project.root))["gate"] == before
    assert before["verdict"] == "BLOCKED"
    assert store.latest(subject_id) is None
    assert store.latest_for("formal_goal", subject_id) is None
    assert store.latest_for("change_certification", subject_id)["run_id"] == record["run_id"]


def test_portable_receipt_schema_separates_shadow_and_authoritative_fields(tmp_path):
    project, store, queued = _queued(tmp_path)
    store.request_cancel(queued["run_id"])
    record = execute(project, queued["run_id"], store=store)
    shadow = read_json(store.run_dir(record["run_id"]) / "receipt.json")
    authority = {
        "kind": "target_policy", "target_commit": "a" * 40, "target_tree": "b" * 40,
        "manifest_blob_oid": "c" * 40, "manifest": {}, "implementation": {},
    }
    for name, value in {
        "authority": authority, "check_digest": "a" * 64, "backend": {},
        "policy_sha256": "a" * 64, "package_sha256": "a" * 64, "checker_sha256": "a" * 64,
    }.items():
        with pytest.raises(SchemaValidationError):
            validate("receipt", EVIDENCE_RECORD, {**shadow, name: value})
    shadow_only = (
        "target_source", "trusted_target_oid", "change_request", "bound_request_sha256",
        "method_plan", "implementation", "source_proof", "approval_authority",
    )
    for kind in ("registered_check", "formal_goal"):
        receipt = {k: v for k, v in shadow.items() if k not in (*shadow_only, "method", "category", "scope")}
        receipt["subject"] = {"kind": kind, "id": "required"}
        if kind == "registered_check":
            receipt.update(check_digest="a" * 64, backend={})
        else:
            receipt.update(policy_sha256="a" * 64, package_sha256="b" * 64,
                           checker_sha256="c" * 64, method="verus_source",
                           category="source_verification", scope="required_api", assurance="trusted_engine")
        validate("receipt", EVIDENCE_RECORD, receipt)
        validate("receipt", EVIDENCE_RECORD, {**receipt, "authority": authority})
        for name in shadow_only:
            with pytest.raises(SchemaValidationError):
                validate("receipt", EVIDENCE_RECORD, {**receipt, name: shadow[name]})
        for name in ("method", "category", "scope"):
            with pytest.raises(SchemaValidationError):
                validate("receipt", EVIDENCE_RECORD, {**receipt, name: shadow[name]})


@pytest.mark.parametrize("cancelled,expected", [(False, "FAIL"), (True, "BLOCKED")])
def test_shadow_failure_does_not_block_same_named_passing_required_check(tmp_path, cancelled, expected):
    project, store, queued = _queued(tmp_path, delta=2)
    subject_id = queued["subject"]["id"]
    (project.root / "required.py").write_text(DRIVER + 'report([("bound", True, "required passed")])\n')
    check = scenario_check(subject_id, "required.py", scenarios=["bound"], inputs=["required.py"])
    write_json(project.manifest_path, {"schema_version": 3, "description": "isolation", "checks": [check]})
    assert vkit(project.root, "accept", "--yes")[0] == 0
    registered = runner.prepare(project, parse_manifest(project), subject_id, store=store)
    registered = execute(project, registered["run_id"], store=store)
    assert registered["outcome"]["result"] == "PASS", registered["outcome"]
    before = query.status(query.open_context(project.root))["gate"]
    assert before["verdict"] == "READY"
    if cancelled:
        store.request_cancel(queued["run_id"])
    shadow = execute(project, queued["run_id"], store=store)
    assert shadow["outcome"]["result"] == expected, shadow["outcome"]
    assert query.status(query.open_context(project.root))["gate"] == before
    assert store.latest(subject_id)["run_id"] == registered["run_id"]
    assert store.latest_for("change_certification", subject_id)["run_id"] == shadow["run_id"]


def test_detached_shadow_worker_uses_shared_wait_lifecycle(tmp_path):
    project, store, queued = _queued(tmp_path)
    jobs._launch(project, store, queued)
    record = jobs.wait(store, queued["run_id"], timeout_seconds=60)
    assert record["state"] == "done", record
    assert record["outcome"]["result"] == "PASS", record["outcome"]
    assert validate_record(record, store.run_dir(record["run_id"]))
