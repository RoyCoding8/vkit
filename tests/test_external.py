from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from vkit.inputs import canonical_digest, file_digest
from vkit.manifest import parse_manifest_data
from vkit.outcome import Blocked, Passed
from vkit.paths import Project
from vkit.verifiers.external import ExternalProtocolError, execute, identity, replay


def test_external_refuses_a_snapshot_root_link_or_windows_junction(tmp_path):
    root, raw, manifest, check, _, _ = _project(tmp_path)
    run_dir = _run_dir(root, "run-linked")
    job = _job(root, raw, manifest, check)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copyfile(root / "spec.json", outside / "spec.json")
    snapshot = run_dir / "snapshot"
    shutil.rmtree(snapshot)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(snapshot))
    else:
        snapshot.symlink_to(outside, target_is_directory=True)
    try:
        outcome, _ = execute(check, run_dir, job, lambda: False)
        assert isinstance(outcome, Blocked)
        assert not (run_dir / "producer-request.json").exists()
    finally:
        if os.name == "nt":
            snapshot.rmdir()
        else:
            snapshot.unlink()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(tmp_path: Path, *, producer_mode: str = "write", checker_mode: str = "accept"):
    root = tmp_path / "project"
    root.mkdir()
    spec = root / "spec.json"
    spec.write_text('{"theorem":"T"}\n', encoding="utf-8")
    producer = root / "producer.py"
    producer.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        f"mode = {producer_mode!r}\n"
        "if mode != 'missing':\n"
        "    evidence = Path(request['evidence_directory']) / 'proof.json'\n"
        "    evidence.write_text(json.dumps({'job_id': request['job']['job_id']}), encoding='utf-8')\n"
        "if mode == 'mutate_snapshot':\n"
        "    Path(request['specification']['snapshot_path']).write_text('changed\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    checker = root / "checker.py"
    checker.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "job = request['job']\n"
        f"mode = {checker_mode!r}\n"
        "if mode == 'mutate_evidence':\n"
        "    (Path(request['evidence_directory']) / 'proof.json').write_text('changed\\n', encoding='utf-8')\n"
        "result = 'rejected' if mode == 'reject' else ('unknown' if mode == 'unknown' else 'accepted')\n"
        "obligations = [{'name': name, 'result': result, 'evidence': ['proof.json']} "
        "for name in request['obligations']]\n"
        "receipt = {\n"
        "    'schema_version': 1,\n"
        "    'job_id': '0' * 64 if mode == 'wrong_claim' else job['job_id'],\n"
        "    'run_id': job['run_id'],\n"
        "    'check_id': job['check_id'],\n"
        "    'check_digest': job['check_digest'],\n"
        "    'specification_digest': request['specification']['sha256'],\n"
        "    'identity': request['identity'],\n"
        "    'evidence_digest': '0' * 64 if mode == 'wrong_evidence_digest' else request['evidence_bundle']['digest'],\n"
        "    'verdict': result,\n"
        "    'obligations': [] if mode == 'missing_obligation' else obligations,\n"
        "}\n"
        "Path(sys.argv[2]).write_text(json.dumps(receipt), encoding='utf-8')\n",
        encoding="utf-8",
    )
    raw = {
        "schema_version": 3,
        "description": "external verifier test",
        "checks": [
            {
                "id": "formal",
                "kind": "external",
                "timeout_seconds": 20,
                "artifact": "external-result.json",
                "subject": {"paths": ["spec.json"], "digest": None},
                "claim_id": "claim.formal",
                "inputs": ["spec.json"],
                "specification": "spec.json",
                "obligations": ["T"],
                "assumptions": ["the listed definitions"],
                "model_scope": "all values in the frozen specification",
                "assurance": "kernel_proof",
                "evidence_artifacts": ["proof.json"],
                "backend": {
                    "executable": {"path": sys.executable, "sha256": _sha256(Path(sys.executable))},
                    "argv": [str(producer), "{{request}}"],
                    "dependencies": [{"path": str(producer), "sha256": _sha256(producer)}],
                },
                "checker": {
                    "executable": {"path": sys.executable, "sha256": _sha256(Path(sys.executable))},
                    "argv": [str(checker), "{{request}}", "{{receipt}}"],
                    "dependencies": [{"path": str(checker), "sha256": _sha256(checker)}],
                },
            }
        ],
    }
    project = Project(root.resolve(), root / ".git")
    manifest = parse_manifest_data(raw, project=project, origin="test manifest")
    check = manifest.require("formal")
    return root, raw, manifest, check, producer, checker


def _job(root: Path, raw: dict, manifest, check, run_id: str = "run-1") -> dict:
    digest = file_digest(root / "spec.json")
    files = [{"path": "spec.json", "sha256": digest}]
    return {
        "schema_version": 1,
        "job_id": canonical_digest({"check": check.id, "input": digest}),
        "run_id": run_id,
        "check_id": check.id,
        "check_digest": manifest.digest(check.id),
        "inputs": {"files": files, "digest": canonical_digest([["spec.json", digest]])},
        "backend": identity(check),
        "entry": raw["checks"][0],
    }


def _run_dir(root: Path, name: str) -> Path:
    run_dir = root.parent / name
    snapshot = run_dir / "snapshot"
    snapshot.mkdir(parents=True)
    shutil.copyfile(root / "spec.json", snapshot / "spec.json")
    return run_dir


def test_external_executes_producer_then_independent_checker_and_records_receipt(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path)
    run_dir = _run_dir(root, "run-1")
    job = _job(root, raw, manifest, check)

    outcome, evidence = execute(check, run_dir, job, lambda: False)

    assert isinstance(outcome, Passed)
    assert evidence["obligations"] == [
        {"name": "T", "result": "accepted", "evidence": ["proof.json"]}
    ]
    assert evidence["evidence_bundle"]["files"] == [
        {"path": "proof.json", "sha256": _sha256(run_dir / "evidence" / "proof.json")}
    ]
    assert evidence["checker_receipt"]["sha256"] == _sha256(run_dir / "checker-receipt.json")
    assert json.loads((run_dir / check.artifact_name).read_text(encoding="utf-8")) == evidence


@pytest.mark.parametrize("checker_mode", ["wrong_claim", "missing_obligation"])
def test_external_rejects_receipts_not_bound_to_every_approved_obligation(tmp_path: Path, checker_mode: str):
    root, raw, manifest, check, _, _ = _project(tmp_path, checker_mode=checker_mode)
    run_dir = _run_dir(root, "run-1")
    job = _job(root, raw, manifest, check)

    outcome, _ = execute(check, run_dir, job, lambda: False)

    assert isinstance(outcome, Blocked)


def test_external_requires_every_declared_artifact(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path, producer_mode="missing")
    run_dir = _run_dir(root, "run-1")

    outcome, _ = execute(check, run_dir, _job(root, raw, manifest, check), lambda: False)

    assert isinstance(outcome, Blocked)


def test_external_rejection_is_inconclusive_not_a_formal_counterexample(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path, checker_mode="reject")
    run_dir = _run_dir(root, "run-1")

    outcome, evidence = execute(check, run_dir, _job(root, raw, manifest, check), lambda: False)

    assert isinstance(outcome, Blocked)
    assert evidence["obligations"][0]["result"] == "rejected"


def test_external_replay_ignores_old_receipt_and_uses_current_checker(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path)
    source = _run_dir(root, "run-1")
    first_job = _job(root, raw, manifest, check, "run-1")
    first_outcome, _ = execute(check, source, first_job, lambda: False)
    assert isinstance(first_outcome, Passed)
    (source / "checker-receipt.json").write_text('{"verdict":"accepted"}', encoding="utf-8")

    replay_dir = _run_dir(root, "run-2")
    replay_job = _job(root, raw, manifest, check, "run-2")
    outcome, evidence = replay(check, source, replay_dir, replay_job, lambda: False)

    assert isinstance(outcome, Passed)
    assert evidence["checker_receipt"]["document"]["run_id"] == "run-2"
    assert (replay_dir / "evidence" / "proof.json").is_file()


def test_external_fails_closed_when_pinned_checker_changes(tmp_path: Path):
    root, raw, manifest, check, _, checker = _project(tmp_path)
    run_dir = _run_dir(root, "run-1")
    job = _job(root, raw, manifest, check)
    checker.write_text(checker.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    outcome, _ = execute(check, run_dir, job, lambda: False)

    assert isinstance(outcome, Blocked)
    with pytest.raises(ExternalProtocolError, match="SHA-256"):
        identity(check)


def test_external_rejects_unpinned_absolute_checker_script(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path)
    job = _job(root, raw, manifest, check)
    raw["checks"][0]["checker"]["dependencies"] = []
    manifest = parse_manifest_data(raw, project=Project(root.resolve(), root / ".git"), origin="test manifest")
    check = manifest.require("formal")
    run_dir = _run_dir(root, "run-1")

    outcome, _ = execute(check, run_dir, job, lambda: False)

    assert isinstance(outcome, Blocked)
    assert not (run_dir / "evidence" / "proof.json").exists()


def test_external_blocks_when_producer_mutates_the_frozen_snapshot(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path, producer_mode="mutate_snapshot")
    run_dir = _run_dir(root, "run-1")

    outcome, _ = execute(check, run_dir, _job(root, raw, manifest, check), lambda: False)

    assert isinstance(outcome, Blocked)


def test_external_blocks_when_checker_mutates_evidence(tmp_path: Path):
    root, raw, manifest, check, _, _ = _project(tmp_path, checker_mode="mutate_evidence")
    run_dir = _run_dir(root, "run-1")

    outcome, _ = execute(check, run_dir, _job(root, raw, manifest, check), lambda: False)

    assert isinstance(outcome, Blocked)


@pytest.mark.parametrize("checker_mode", ["unknown", "wrong_evidence_digest"])
def test_external_blocks_unknown_or_misbound_evidence_receipts(tmp_path: Path, checker_mode: str):
    root, raw, manifest, check, _, _ = _project(tmp_path, checker_mode=checker_mode)
    run_dir = _run_dir(root, "run-1")

    outcome, _ = execute(check, run_dir, _job(root, raw, manifest, check), lambda: False)

    assert isinstance(outcome, Blocked)
