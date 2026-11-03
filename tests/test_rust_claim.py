from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from test_rust_relational import _fixture
from vkit import kani_execution
from vkit.certification import (
    DelegationBasis,
    FiniteContract,
    NearMiss,
    OutputRow,
    check_delegation,
)
from vkit.kani_result import classify_kani_result
from vkit.paths import open_project
from vkit.rust_relational import (
    RustClaimControlProgram,
    RustClaimProgram,
    RustRelationalProgram,
    prepare_rust_claim_program,
    prepare_rust_relational,
)

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_rust")

FIXTURES = Path(__file__).parent / "fixtures" / "kani-results"


def _delegated_inputs(tmp_path):
    root, target, request = _fixture(tmp_path)
    ordinary = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)
    intent = ordinary.bound.intent
    domain = intent.contract.domain
    claim = FiniteContract(
        domain,
        intent.contract.admissible_inputs,
        (OutputRow("0", ("1",)), OutputRow("1", ("2",))),
    )
    baseline = FiniteContract(
        domain,
        intent.contract.admissible_inputs,
        tuple(OutputRow(token, domain.observations) for token in intent.contract.admissible_inputs),
    )
    basis = DelegationBasis(
        intent.digest,
        baseline,
        FiniteContract(domain, claim.admissible_inputs, claim.rows),
    )
    near_miss = NearMiss("1", "0")
    derivation = check_delegation(intent, basis, claim, near_miss)
    request = replace(ordinary.bound.request, claim=claim)
    bound = replace(ordinary.bound, request=request, delegation=derivation)
    return bound, ordinary.base, ordinary.candidate


def _delegated_program(tmp_path, purpose="candidate_claim_satisfaction"):
    bound, base, candidate = _delegated_inputs(tmp_path)
    return prepare_rust_claim_program(bound, base, candidate, purpose=purpose)


def _qualified_fixture(name: str, harness: bytes) -> tuple[bytes, int]:
    raw = json.loads((FIXTURES / name / "kani-results.json").read_bytes())
    lines = harness.decode("utf-8").splitlines()
    assertion_lines = {
        match.group(1): str(index)
        for index, line in enumerate(lines, start=1)
        if (match := re.fullmatch(r'\s*assert!\(.*?,\s*"([a-z_]+)"\);\s*', line))
    }
    for check in raw["verification_results"]["results"][0]["checks"]:
        description = check.get("description", "")
        assertion_name = (
            description[1:-1]
            if description.startswith('"') and description.endswith('"')
            else None
        )
        if assertion_name in assertion_lines:
            check["location"]["line"] = assertion_lines[assertion_name]
    source = raw["harness_metadata"][0]["source"]
    source["start_line"] = 1
    source["end_line"] = len(lines)
    return json.dumps(raw).encode(), 1 if name == "regression" else 0


def test_claim_and_control_programs_have_distinct_bound_harnesses(tmp_path):
    bound, base, candidate = _delegated_inputs(tmp_path)
    satisfaction = prepare_rust_claim_program(
        bound, base, candidate, purpose="candidate_claim_satisfaction",
    )
    control = prepare_rust_claim_program(
        bound, base, candidate, purpose="near_miss_control",
    )

    assert isinstance(satisfaction, RustClaimProgram)
    assert isinstance(control, RustClaimControlProgram)
    assert satisfaction.purpose == "candidate_claim_satisfaction"
    assert control.purpose == "near_miss_control"
    assert satisfaction.digest != control.digest
    assert satisfaction.to_json()["scope"] == "rust_claim_proof_program"
    assert satisfaction.to_json()["basis_sha256"] == control.to_json()["basis_sha256"]
    assert satisfaction.to_json()["claim_sha256"] == control.to_json()["claim_sha256"]
    assert satisfaction.to_json()["domain_sha256"] == control.to_json()["domain_sha256"]
    assert satisfaction.to_json()["model_sha256"] == control.to_json()["model_sha256"]
    assert satisfaction.to_json()["harness_sha256"] == hashlib.sha256(satisfaction.harness).hexdigest()
    assert control.to_json()["harness_sha256"] == hashlib.sha256(control.harness).hexdigest()
    assert satisfaction.to_json()["base_capture_sha256"] == base.digest
    assert control.to_json()["base_capture_sha256"] == base.digest
    assert satisfaction.to_json()["candidate_capture_sha256"] == candidate.digest
    assert control.to_json()["candidate_capture_sha256"] == candidate.digest
    assert satisfaction.to_json()["native_compiler_correspondence"] is False
    assert satisfaction.to_json()["approval_authority"] is False

    positive = satisfaction.harness.decode("utf-8")
    negative = control.harness.decode("utf-8")
    assert 'let actual_after: u32 = vkit_candidate::convert(x);' in positive
    assert 'let after: u32 = actual_after;' in positive
    assert 'assert!(vkit_standing(x, actual_after), "candidate_standing_intent");' in positive
    assert 'assert!(vkit_claim(x, after), "relational_policy");' in positive
    assert "if x == 1u32 { 0u32 } else { actual_after }" in negative
    assert 'assert!(vkit_standing(x, actual_after), "candidate_standing_intent");' in negative
    assert 'assert!(vkit_claim(x, after), "relational_policy");' in negative
    assert positive.count('"base_observation_model"') == 1
    assert positive.count('"candidate_observation_model"') == 1
    assert positive.count('"candidate_standing_intent"') == 1
    assert positive.count('"relational_policy"') == 1


@pytest.mark.parametrize(
    ("fixture", "purpose", "expected"),
    [
        ("preservation", "candidate_claim_satisfaction", "verified"),
        ("regression", "near_miss_control", "proof_rejected"),
    ],
)
def test_claim_harnesses_remain_inside_strict_kani_decoder(
    tmp_path, fixture, purpose, expected,
):
    program = _delegated_program(tmp_path, purpose)
    raw, exit_code = _qualified_fixture(fixture, program.harness)

    outcome = classify_kani_result(raw, exit_code=exit_code, harness=program.harness)

    assert outcome.kind == expected
    if purpose == "near_miss_control":
        assert outcome.reason == "named_relational_policy_assertion_failed"


def test_claim_factory_rejects_unbound_ordinary_program(tmp_path):
    root, target, request = _fixture(tmp_path)
    ordinary = prepare_rust_relational(open_project(root), request, trusted_target_oid=target)

    with pytest.raises(ValueError, match="target-bound delegation derivation"):
        prepare_rust_claim_program(
            ordinary.bound, ordinary.base, ordinary.candidate,
            purpose="candidate_claim_satisfaction",
        )


def test_claim_and_relational_executor_entry_points_reject_cross_purpose_programs(tmp_path):
    bound, base, candidate = _delegated_inputs(tmp_path)
    satisfaction = prepare_rust_claim_program(
        bound, base, candidate, purpose="candidate_claim_satisfaction",
    )
    ordinary = RustRelationalProgram(
        bound, base, candidate, satisfaction.policy, satisfaction.policy_identity,
        satisfaction.policy_bytes, satisfaction.binding, satisfaction.base_profile,
        satisfaction.candidate_profile, satisfaction.harness,
    )

    with pytest.raises(TypeError, match="relational program"):
        kani_execution.run_kani_relational(
            satisfaction, toolchain=None, output_dir=tmp_path / "ordinary-run",
        )
    with pytest.raises(TypeError, match="purpose-bound claim program"):
        kani_execution.run_kani_claim(
            ordinary, toolchain=None, output_dir=tmp_path / "claim-run",
        )
