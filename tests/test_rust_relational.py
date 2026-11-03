from __future__ import annotations

import hashlib
import json

import pytest

from helpers import git
from test_paired import _git_output, _json_bytes, _repository
from vkit.paths import open_project
from vkit.rust_relational import prepare_rust_relational
from vkit.rust_source import UnsupportedRustSource

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_rust")


def _fixture(tmp_path, **kwargs):
    return _repository(
        tmp_path, language="rust", candidate_delta=1,
        outputs={"0": ["0", "1", "2"], "1": ["1", "2"]}, **kwargs,
    )


def _protect(root, target, request, guards):
    candidate_bytes = (root / "candidate.rs").read_bytes()
    git(root, "checkout", "--detach", target)
    path = root / f"verification/change-intents/differences/{request['intent_sha256']}.v1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(_json_bytes({
        "schema_version": 1, "kind": "finite_differences",
        "intent_sha256": request["intent_sha256"], "guards": guards,
    }))
    git(root, "add", ".")
    git(root, "commit", "-qm", "protect relational policy")
    target = _git_output(root, "rev-parse", "HEAD")
    (root / "candidate.rs").write_bytes(candidate_bytes)
    git(root, "add", "candidate.rs")
    git(root, "commit", "-qm", "candidate source")
    request = dict(request, base_oid=target, candidate_oid=_git_output(root, "rev-parse", "HEAD"))
    return target, request, path


def test_preparer_uses_captured_sources_and_retains_complete_identity(tmp_path):
    root, target, request = _fixture(tmp_path)
    captured = (root / "candidate.rs").read_bytes()
    (root / "candidate.rs").write_text('compile_error!("live checkout must not be read");')
    program = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
    repeated = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
    manifest = program.to_json()
    assert manifest["scope"] == "rust_relational_obligation"
    assert manifest["executed"] is False
    assert manifest["engine_toolchain_bound"] is False
    assert manifest["native_environment_validated"] is False
    assert manifest["source_proof"] is False
    assert manifest["approval_authority"] is False
    assert manifest["base_capture_sha256"] == program.bound.base_capture_sha256
    assert manifest["candidate_capture_sha256"] == program.bound.candidate_capture_sha256
    assert manifest["harness_sha256"] == hashlib.sha256(program.harness).hexdigest()
    assert program.candidate_profile.source_sha256 == hashlib.sha256(captured).hexdigest()
    assert next(file.content for file in program.candidate.files if file.path == "candidate.rs") == captured
    assert manifest["required_assertions"] == [
        "base_observation_model", "candidate_observation_model",
        "candidate_standing_intent", "relational_policy",
    ]
    assert program.digest == repeated.digest
    assert program.harness == repeated.harness
    assert b'#[path = "base/candidate.rs"] mod vkit_base;' in program.harness
    assert b'#[path = "candidate/candidate.rs"] mod vkit_candidate;' in program.harness
    assert str(root).encode() not in program.harness


def test_guarded_obligation_uses_intersection_and_preserves_standing_rules(tmp_path):
    root, target, request = _fixture(tmp_path)
    target, request, _ = _protect(root, target, request, [
        {"id": "feature", "requirements": [{"input": "0", "outputs": ["1", "2"]}]},
        {"id": "narrow", "requirements": [{"input": "0", "outputs": ["2"]}]},
    ])
    program = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
    harness = program.harness.decode()
    assert "0u32 => matches!(after, 2u32)," in harness
    assert "1u32 => matches!(y, 1u32 | 2u32)," in harness
    assert '_ => before == after || (!vkit_standing(x, before) && vkit_standing(x, after)),' in harness
    assert 'assert!(vkit_standing(x, after), "candidate_standing_intent");' in harness
    assert 'assert!(matches!(before, 0u32 | 1u32 | 2u32 | 3u32), "base_observation_model");' in harness
    assert dict(program.policy_identity)["default"] is False
    assert json.loads(program.policy_bytes)["guards"][1]["id"] == "narrow"


def test_request_cannot_exclude_accepted_input(tmp_path):
    root, target, request = _fixture(tmp_path)
    request["claim"]["contract"]["admissible_inputs"] = ["0"]
    request["claim"]["contract"]["allowed_outputs"] = [
        {"input": "0", "outputs": ["0", "1", "2", "3"]},
    ]
    with pytest.raises(ValueError, match="not entailed"):
        prepare_rust_relational(open_project(root), request, trusted_target_oid=target)


def test_weaker_requested_outputs_do_not_weaken_standing_obligation(tmp_path):
    root, target, request = _fixture(tmp_path)
    request["claim"]["contract"]["allowed_outputs"] = [
        {"input": token, "outputs": ["0", "1", "2", "3"]} for token in ("0", "1")
    ]
    program = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
    assert program.to_json()["admissible_inputs"] == ["0", "1"]
    assert b"1u32 => matches!(y, 1u32 | 2u32)," in program.harness
    assert b"1u32 => matches!(y, 0u32 | 1u32 | 2u32 | 3u32)," not in program.harness


@pytest.mark.parametrize("source", [
    "pub fn convert(value: u32) -> u32 { if cfg!(kani) { value } else { value + 1 } }",
    "#[cfg(kani)] pub fn convert(value: u32) -> u32 { value }",
])
def test_configuration_forks_are_rejected_before_obligation_generation(tmp_path, source):
    root, target, request = _fixture(tmp_path, candidate_source=source)
    with pytest.raises(UnsupportedRustSource):
        prepare_rust_relational(open_project(root), request, trusted_target_oid=target)


def test_candidate_cannot_replace_protected_guard_policy(tmp_path):
    root, target, request = _fixture(tmp_path)
    target, request, path = _protect(root, target, request, [])
    path.write_bytes(_json_bytes({
        "schema_version": 1, "kind": "finite_differences", "intent_sha256": request["intent_sha256"],
        "guards": [{"id": "self-approved", "requirements": [{"input": "0", "outputs": ["1"]}]}],
    }))
    git(root, "add", ".")
    git(root, "commit", "-qm", "candidate changes policy")
    request["candidate_oid"] = _git_output(root, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="candidate difference policy differs"):
        prepare_rust_relational(open_project(root), request, trusted_target_oid=target)


@pytest.mark.parametrize(("role", "declaration"), [
    ("codec", {"schema_version": True, "kind": "u32_le"}),
    ("observation", {"schema_version": 1, "kind": "projected_return"}),
])
def test_unqualified_semantic_declarations_are_unsupported(tmp_path, role, declaration):
    root, target, request = _fixture(tmp_path, semantic_changes={role: declaration})
    with pytest.raises(UnsupportedRustSource, match=f"unsupported {role}"):
        prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
