from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from helpers import git
from test_paired import _git_output, _repository
from vkit import kani_execution, query, runner
from vkit.certification import DelegationBasis, FiniteContract, OutputRow
from vkit.evidence import seal, validate_record
from vkit.inputs import canonical_digest
from vkit.intent import _prepare_change_request
from vkit.kani_toolchain import KaniToolchain
from vkit.paired import _json_bytes
from vkit.paths import open_project
from vkit.proc import Exit
from vkit.store import Store, read_json, write_json


def _delegated_repository(tmp_path, *, narrow=False):
    root, target, request = _repository(
        tmp_path, language="rust", candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    bound, _, _ = _prepare_change_request(open_project(root), request, trusted_target_oid=target)
    contract = bound.intent.contract
    broad = FiniteContract(contract.domain, contract.admissible_inputs, tuple(
        OutputRow(token, contract.domain.observations) for token in contract.admissible_inputs
    ))
    assumptions = broad
    if narrow:
        assumptions = FiniteContract(contract.domain, contract.admissible_inputs,
                                     (OutputRow("0", ("1",)), OutputRow("1", ("1", "2"))))
        request["claim"]["contract"] = assumptions.to_json()
    basis = DelegationBasis(bound.intent.digest, broad, assumptions)
    git(root, "checkout", "--detach", target)
    write_json(root / f"verification/change-claims/{basis.digest}.v1.json", basis.to_json())
    write_json(root / "verification/change-claims/accepted.v1.json",
               {"schema_version": 1, "accepted": [basis.digest]})
    git(root, "add", ".")
    git(root, "commit", "-qm", "accept delegation basis")
    target = _git_output(root, "rev-parse", "HEAD")
    request.update(base_oid=target, candidate_oid=target, delegation={
        "basis_sha256": basis.digest, "near_miss": {"input": "0", "output": "2"},
    })
    return root, target, request


def _constructed_export(harness, *, rejected):
    fixture = "regression" if rejected else "preservation"
    raw = read_json(Path(__file__).parent / f"fixtures/kani-results/{fixture}/kani-results.json")
    lines = harness.decode().splitlines()
    raw["harness_metadata"][0]["source"].update(start_line=1, end_line=len(lines))
    locations = {}
    for index, line in enumerate(lines, 1):
        match = re.search(r'assert!\(.*?, "([a-z_]+)"\);', line)
        if match:
            locations[match[1]] = str(index)
    for check in raw["verification_results"]["results"][0]["checks"]:
        name = check["description"].strip('"')
        if name in locations:
            check["location"]["line"] = locations[name]
    return _json_bytes(raw)


@pytest.fixture
def delegated_job(tmp_path, monkeypatch, request, delegated_projects):
    narrow = getattr(request, "param", False)
    root, target, change_request = delegated_projects[narrow]
    monkeypatch.setattr(kani_execution.platform, "system", lambda: "Linux")
    monkeypatch.setattr(kani_execution.platform, "machine", lambda: "x86_64")
    manifests = tuple((name + ".jsonl", b"{}\n") for name in (
        "kani", "rust", "cargo-bin", "host-libraries",
    ))
    digest = hashlib.sha256(b"{}\n").hexdigest()
    pin = KaniToolchain(tmp_path / "kani", tmp_path / "rustup", tmp_path / "cargo",
                        digest, digest, "c" * 64)
    monkeypatch.setattr(kani_execution, "validate_kani_toolchain", lambda *a, **k: manifests)
    native_process = kani_execution.proc.run

    def constructed_process(argv, *, cwd, stdout_path, stderr_path, **kwargs):
        if len(argv) < 2 or argv[1] != "harness.rs":
            return native_process(argv, cwd=cwd, stdout_path=stdout_path, stderr_path=stderr_path, **kwargs)
        harness = (cwd / "harness.rs").read_bytes()
        rejected = b"let after: u32 = if x == " in harness or (narrow and b"fn vkit_claim" in harness)
        (cwd / "kani-results.json").write_bytes(_constructed_export(harness, rejected=rejected))
        stdout_path.write_bytes(b"constructed engine boundary, no solver executed\n")
        stderr_path.write_bytes(b"")
        return Exit(1 if rejected else 0)

    monkeypatch.setattr(kani_execution.proc, "run", constructed_process)
    project = open_project(root)
    store = Store.of(project)
    queued = runner.prepare_change(project, change_request, trusted_target_oid=target,
                                   requested_method="rust_kani", kani_toolchain=pin, store=store)
    return project, store, queued


@pytest.fixture(scope="module")
def delegated_projects(tmp_path_factory):
    return {narrow: _delegated_repository(tmp_path_factory.mktemp(f"delegated-{narrow}"), narrow=narrow)
            for narrow in (False, True)}


def _rehash_delegated_evidence(record, root):
    for path in (root / "delegated-claim").glob("*/report.json"):
        report = read_json(path)
        for ref in report["artifacts"]:
            ref["sha256"] = hashlib.sha256((path.parent / ref["path"]).read_bytes()).hexdigest()
        report["program_sha256"] = canonical_digest(read_json(path.parent / "program.json"))
        write_json(path, report)
    path = root / "claim-acceptance.v1.json"
    accepted = read_json(path)
    for field in ("satisfaction", "near_miss_control"):
        ref = accepted[field]
        ref["sha256"] = hashlib.sha256((root / ref["path"]).read_bytes()).hexdigest()
    write_json(path, accepted)
    report = record["verification"]["change_certification"]
    report["delegated_claim"]["acceptance"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(root / "change-certification.json", report)


def test_constructed_engine_boundary_publishes_only_model_scoped_claim_acceptance(delegated_job):
    project, store, queued = delegated_job
    record = runner.execute(project, queued["run_id"], store=store)
    directory = store.run_dir(record["run_id"])
    report = record["verification"]["change_certification"]
    assert report["delegated_claim"]["status"] == "accepted", report["delegated_claim"]
    accepted = read_json(directory / "change-output/claim-acceptance.v1.json")
    assert accepted["accepted_by"] == "agent_strengthening"
    assert accepted["scope"] == "restricted_captured_rust_kani_model"
    assert accepted["approval_authority"] is False and accepted["source_proof"] is False
    assert validate_record(record, directory)
    assert record["claim_outcome"] == "unknown"
    assert store.approved() == {}
    ctx = query.open_context(project.root)
    assert query.run_view(ctx, record["run_id"])["summary"]["change_certification"]["delegated_claim"]["status"] == "accepted"
    assert query.run_summary(record)["change_certification"]["delegated_claim"]["status"] == "unknown"


@pytest.mark.parametrize("delegated_job", [True], indirect=True)
def test_relational_success_does_not_replace_independent_claim_satisfaction(delegated_job):
    project, store, queued = delegated_job
    record = runner.execute(project, queued["run_id"], store=store)
    report = record["verification"]["change_certification"]
    assert report["status"] == "model_verified"
    assert report["delegated_claim"] == {
        "status": "refuted", "reason": "named_relational_policy_assertion_failed",
    }
    directory = store.run_dir(record["run_id"])
    assert validate_record(record, directory)
    assert not (directory / "change-output/claim-acceptance.v1.json").exists()


@pytest.mark.parametrize("fault", ["control_passes", "incomplete_export", "cancelled"])
def test_incomplete_or_non_discriminating_proofs_never_accept(delegated_job, monkeypatch, fault):
    project, store, queued = delegated_job
    original = kani_execution.proc.run

    def faulty_process(argv, **kwargs):
        result = original(argv, **kwargs)
        if len(argv) < 2 or argv[1] != "harness.rs":
            return result
        cwd = kwargs["cwd"]
        harness = (cwd / "harness.rs").read_bytes()
        if b"fn vkit_claim" not in harness:
            return result
        if fault == "cancelled":
            store.request_cancel(queued["run_id"])
        elif fault == "incomplete_export":
            write_json(cwd / "kani-results.json", {})
        elif b"let after: u32 = if x == " in harness:
            (cwd / "kani-results.json").write_bytes(_constructed_export(harness, rejected=False))
            return Exit(0)
        return result

    monkeypatch.setattr(kani_execution.proc, "run", faulty_process)
    record = runner.execute(project, queued["run_id"], store=store)
    report = record["verification"]["change_certification"]
    assert report["delegated_claim"]["status"] == "unknown"
    if fault == "control_passes":
        assert report["delegated_claim"]["reason"] == "all_qualified_properties_succeeded"
    elif fault == "cancelled":
        assert report["status"] == "unknown" and report["stopped"] == "cancelled"
    else:
        assert report["delegated_claim"]["reason"] == "malformed_or_inconsistent_export"
    directory = store.run_dir(record["run_id"])
    assert validate_record(record, directory)
    assert not (directory / "change-output/claim-acceptance.v1.json").exists()


@pytest.mark.parametrize("attack", [
    "cancel", "purpose", "harness", "export", "claim", "checker", "ordinary_proof",
])
def test_rehashed_receipt_cannot_replace_delegated_claim_evidence(delegated_job, attack):
    project, store, queued = delegated_job
    record = runner.execute(project, queued["run_id"], store=store)
    directory = store.run_dir(record["run_id"])
    assert validate_record(record, directory)
    root = directory / "change-output"
    proof = root / "delegated-claim/candidate_claim_satisfaction"
    if attack == "cancel":
        store.request_cancel(record["run_id"])
    elif attack == "harness":
        (proof / "engine/harness.rs").write_bytes(b"fn main() {}\n")
    elif attack == "export":
        write_json(proof / "engine/kani-results.json", {})
    elif attack == "ordinary_proof":
        write_json(proof / "program.json", read_json(root / "01-rust_kani/program.json"))
    else:
        path = root / "claim-acceptance.v1.json" if attack == "claim" else proof / (
            "program.json" if attack == "purpose" else "implementation.json")
        raw = read_json(path)
        if attack == "purpose":
            raw["purpose"] = "near_miss_control"
        elif attack == "claim":
            raw["claim"]["allowed_outputs"][0]["outputs"] = ["2"]
        else:
            raw["kani_result.py"] = "f" * 64
        write_json(path, raw)
    _rehash_delegated_evidence(record, root)
    seal(record, directory)
    store.save(record)
    assert validate_record(record, directory) is False
    shown = query.run_view(query.open_context(project.root), record["run_id"])
    assert shown["summary"]["change_certification"]["delegated_claim"] == {
        "status": "unknown", "reason": "acceptance_evidence_not_validated",
    }
