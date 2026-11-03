from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from helpers import NO_WINDOW, git
from vkit.certification import ChangeCertificationRequest, IntentRevision
from vkit.intent import preflight_change_request
from vkit.paths import open_project
from vkit.schemas import SchemaValidationError


def _revision(artifact_hashes):
    return {
        "schema_version": 1,
        "requirement": {
            "source": "requirements.md#choice",
            "statement": "Either allow or deny is legal.",
        },
        "contract": {
            "domain": {
                "binding": "example::choose",
                "inputs": ["x", "y"],
                "observations": ["allow", "deny", "other"],
                "codec_sha256": artifact_hashes["codec"],
                "observation_sha256": artifact_hashes["observation"],
                "environment_sha256": artifact_hashes["environment"],
            },
            "admissible_inputs": ["x"],
            "allowed_outputs": [{"input": "x", "outputs": ["allow", "deny"]}],
        },
        "artifacts": {
            role: {"path": f"verification/semantics/{role}.json",
                   "sha256": artifact_hashes[role]}
            for role in ("binding", "codec", "observation", "environment")
        },
    }


def _repository(tmp_path, *, object_format="sha1"):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", f"--object-format={object_format}")
    artifact_hashes = {}
    for role in ("binding", "codec", "observation", "environment"):
        content = f'{{"role":"{role}"}}\n'.encode()
        path = root / f"verification/semantics/{role}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifact_hashes[role] = hashlib.sha256(content).hexdigest()

    raw = _revision(artifact_hashes)
    intent = IntentRevision.from_json(raw)
    revision_path = root / f"verification/change-intents/{intent.digest}.v1.json"
    revision_path.parent.mkdir(parents=True, exist_ok=True)
    revision_path.write_text(json.dumps(intent.to_json()), encoding="utf-8")
    accepted = root / "verification/change-intents/accepted.v1.json"
    accepted.write_text(
        json.dumps({"schema_version": 1, "accepted": [intent.digest]}),
        encoding="utf-8",
    )
    (root / "candidate.rs").write_text("pub fn choose() -> bool { true }\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "accept finite intent")
    target_oid = git_output(root, "rev-parse", "HEAD")
    (root / "candidate.rs").write_text("pub fn choose() -> bool { false }\n")
    git(root, "add", "candidate.rs")
    git(root, "commit", "-qm", "change candidate")
    candidate_oid = git_output(root, "rev-parse", "HEAD")
    request = {
        "schema_version": 1,
        "base_oid": target_oid,
        "candidate_oid": candidate_oid,
        "intent_sha256": intent.digest,
        "claim": {"kind": "finite_contract", "contract": intent.contract.to_json()},
    }
    return root, target_oid, candidate_oid, intent, raw, request


def git_output(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.email=t@vkit", "-c", "user.name=t", *args],
        cwd=root,
        text=True,
        **NO_WINDOW,
    ).strip()


def test_intent_revision_round_trip_normalizes_contract_sets():
    hashes = {role: hashlib.sha256(role.encode()).hexdigest()
              for role in ("binding", "codec", "observation", "environment")}
    raw = _revision(hashes)
    raw["contract"]["domain"]["inputs"].reverse()
    raw["contract"]["domain"]["observations"].reverse()

    intent = IntentRevision.from_json(raw)

    assert intent.requirement_source == "requirements.md#choice"
    assert intent.requirement_statement == "Either allow or deny is legal."
    assert intent.to_json()["contract"]["domain"]["inputs"] == ["x", "y"]
    assert intent.digest == IntentRevision.from_json(intent.to_json()).digest


@pytest.mark.parametrize("role", ["codec", "observation", "environment"])
def test_intent_revision_requires_domain_artifact_hash_coupling(role):
    hashes = {name: hashlib.sha256(name.encode()).hexdigest()
              for name in ("binding", "codec", "observation", "environment")}
    raw = _revision(hashes)
    raw["artifacts"][role]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="artifact hashes"):
        IntentRevision.from_json(raw)


def test_intent_revision_rejects_unknown_fields():
    hashes = {role: hashlib.sha256(role.encode()).hexdigest()
              for role in ("binding", "codec", "observation", "environment")}
    raw = _revision(hashes)
    raw["accepted"] = True

    with pytest.raises(SchemaValidationError):
        IntentRevision.from_json(raw)


@pytest.mark.parametrize("object_format,oid_length", [("sha1", 40), ("sha256", 64)])
def test_preflight_binds_real_git_revisions_without_claiming_source_behavior(
    tmp_path, object_format, oid_length
):
    root, target_oid, candidate_oid, intent, raw, request = _repository(
        tmp_path, object_format=object_format
    )

    bound = preflight_change_request(
        open_project(root), request, trusted_target_oid=target_oid
    )

    assert len(bound.request.base_oid) == oid_length
    assert bound.request.candidate_oid == candidate_oid
    assert bound.intent.to_json() == raw
    assert bound.to_json()["scope"] == "request_preflight"
    assert bound.to_json()["candidate_behavior_checked"] is False
    assert bound.digest == preflight_change_request(
        open_project(root), request, trusted_target_oid=target_oid
    ).digest


def test_change_request_json_projection_preserves_semantic_request():
    hashes = {role: hashlib.sha256(role.encode()).hexdigest()
              for role in ("binding", "codec", "observation", "environment")}
    intent = IntentRevision.from_json(_revision(hashes))
    raw = {
        "schema_version": 1,
        "base_oid": "1" * 40,
        "candidate_oid": "2" * 40,
        "intent_sha256": intent.digest,
        "claim": {"kind": "finite_contract", "contract": intent.contract.to_json()},
    }

    request = ChangeCertificationRequest.from_json(raw)

    assert request.to_json() == raw


@pytest.mark.parametrize("record", ["accepted-list", "current-revision"])
@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_preflight_rejects_candidate_intent_authority_drift(tmp_path, record, operation):
    root, target_oid, _, intent, _, request = _repository(tmp_path)
    directory = root / "verification/change-intents"
    path = directory / (
        "accepted.v1.json" if record == "accepted-list" else f"{intent.digest}.v1.json"
    )
    if operation == "delete":
        path.unlink()
    else:
        modified = json.loads(path.read_bytes())
        if record == "accepted-list":
            modified["accepted"].append("0" * 64)
        else:
            modified["requirement"]["statement"] = "Candidate changed the accepted requirement."
        path.write_text(json.dumps(modified), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "change candidate intent authority")
    request["candidate_oid"] = git_output(root, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="candidate .* differs from trusted target"):
        preflight_change_request(open_project(root), request, trusted_target_oid=target_oid)


def test_preflight_allows_unaccepted_intent_proposal_without_granting_authority(tmp_path):
    root, target_oid, _, intent, raw, request = _repository(tmp_path)
    proposal_raw = json.loads(json.dumps(raw))
    proposal_raw["requirement"]["statement"] = "Proposed replacement requirement."
    proposal = IntentRevision.from_json(proposal_raw)
    proposal_path = root / f"verification/change-intents/{proposal.digest}.v1.json"
    proposal_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "propose an unaccepted intent")
    request["candidate_oid"] = git_output(root, "rev-parse", "HEAD")

    bound = preflight_change_request(open_project(root), request, trusted_target_oid=target_oid)

    assert bound.intent.digest == intent.digest
    assert bound.intent.digest != proposal.digest
    assert bound.to_json()["candidate_behavior_checked"] is False
