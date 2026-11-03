import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vkit.kani_result import classify_kani_result

FIXTURES = Path(__file__).parent / "fixtures" / "kani-results"


def _fixture(name: str) -> tuple[bytes, bytes]:
    path = FIXTURES / name
    return (path / "kani-results.json").read_bytes(), (path / "harness.rs").read_bytes()


def _result(raw: bytes, harness: bytes, exit_code: int = 0) -> dict[str, object]:
    return classify_kani_result(raw, exit_code=exit_code, harness=harness).to_json()


def test_decoded_outcome_is_frozen() -> None:
    raw, harness = _fixture("preservation")
    outcome = classify_kani_result(raw, exit_code=0, harness=harness)
    with pytest.raises(FrozenInstanceError):
        outcome.kind = "unknown"


@pytest.mark.parametrize(
    ("name", "exit_code", "kind", "reason"),
    [
        ("preservation", 0, "verified", "all_qualified_properties_succeeded"),
        ("guarded", 0, "verified", "all_qualified_properties_succeeded"),
        ("repair", 0, "verified", "all_qualified_properties_succeeded"),
        ("identity", 0, "verified", "all_qualified_properties_succeeded"),
        ("regression", 1, "proof_rejected", "named_relational_policy_assertion_failed"),
        ("outside-model", 1, "unknown", "observation_model_failed"),
        ("overflow", 1, "unknown", "safety_property_failed"),
    ],
)
def test_retained_actual_kani_outputs(
    name: str, exit_code: int, kind: str, reason: str
) -> None:
    raw, harness = _fixture(name)
    assert _result(raw, harness, exit_code) == {
        "kind": kind,
        "reason": reason,
        "source_proof": False,
        "approval_authority": False,
    }


def test_actual_guarded_harness_derives_its_assertion_location() -> None:
    raw, harness = _fixture("guarded")
    exported = json.loads(raw)
    checks = exported["verification_results"]["results"][0]["checks"]
    relation = next(
        check for check in checks if check["description"] == '"relational_policy"'
    )
    assert relation["location"]["line"] == "22"
    assert _result(raw, harness)["kind"] == "verified"

    relation["location"]["line"] = "20"
    assert _result(json.dumps(exported).encode(), harness)["kind"] == "unknown"


def test_actual_repair_harness_uses_its_own_assertion_line() -> None:
    raw, harness = _fixture("repair")
    exported = json.loads(raw)
    checks = exported["verification_results"]["results"][0]["checks"]
    relation = next(
        check for check in checks if check["description"] == '"relational_policy"'
    )
    assert relation["location"]["line"] == "20"
    assert _result(raw, harness)["kind"] == "verified"


def test_harness_paths_bind_nested_source_properties_and_rust_literals() -> None:
    raw, harness = _fixture("preservation")
    exported = json.loads(raw)
    path = 'candidate/nested/é"custom.rs'
    harness = harness.replace(
        b'#[path = "candidate/candidate.rs"] mod vkit_candidate;',
        b'#[path = "candidate/nested/\\u{e9}\\"custom.rs"] mod vkit_candidate;',
    )
    for check in exported["verification_results"]["results"][0]["checks"]:
        if check["function"].startswith("vkit_candidate::"):
            check["location"]["file"] = path
    assert _result(json.dumps(exported).encode(), harness)["kind"] == "verified"

    candidate_check = next(
        check
        for check in exported["verification_results"]["results"][0]["checks"]
        if check["function"].startswith("vkit_candidate::")
    )
    candidate_check["location"]["file"] = "candidate/candidate.rs"
    assert _result(json.dumps(exported).encode(), harness)["kind"] == "unknown"


def test_mangled_name_and_nonsemantic_metadata_are_not_classification_inputs() -> None:
    raw, harness = _fixture("preservation")
    exported = json.loads(raw)
    exported["harness_metadata"][0]["mangled_name"] = "compiler-internal-name-changed"
    exported["cbmc"][0].pop("cbmc_stats")
    exported["verification_results"]["results"][0]["duration_ms"] += 1
    assert _result(json.dumps(exported).encode(), harness)["kind"] == "verified"


def test_unknown_for_duplicate_json_keys_and_oversized_input() -> None:
    _, harness = _fixture("preservation")
    duplicate = b'{"metadata":{},"metadata":{}}'
    assert _result(duplicate, harness) == {
        "kind": "unknown",
        "reason": "malformed_or_inconsistent_export",
        "source_proof": False,
        "approval_authority": False,
    }
    assert _result(b" " * (2 * 1024 * 1024 + 1), harness)["kind"] == "unknown"
    assert _result(b"\xff", harness)["kind"] == "unknown"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["metadata"].update(kani_version="0.69.0"),
        lambda doc: doc["coverage"].update(enabled=True),
        lambda doc: doc["verification_results"]["summary"].update(failed=1),
        lambda doc: doc["verification_results"]["summary"].update(executed=True),
        lambda doc: doc["property_details"][0]["property_details"].update(passed=True),
        lambda doc: doc["error_details"][0].update(error_type="assertion_failure"),
        lambda doc: doc["verification_results"]["results"][0]["checks"][0].update(id=2),
        lambda doc: doc["verification_results"]["results"][0]["checks"][0].update(
            status="Maybe"
        ),
        lambda doc: doc["verification_results"]["results"][0]["checks"][2][
            "location"
        ].update(line="99"),
    ],
)
def test_mutated_inconsistent_or_unknown_exports_fail_closed(mutate) -> None:
    raw, harness = _fixture("preservation")
    exported = json.loads(raw)
    mutate(exported)
    result = _result(json.dumps(exported).encode(), harness)
    assert result["kind"] == "unknown"
    assert result["source_proof"] is False
    assert result["approval_authority"] is False


def test_mutated_harness_line_does_not_rebind_existing_export() -> None:
    raw, harness = _fixture("preservation")
    mutated_harness = harness.replace(
        b'assert!(matches!(before, 0u32 | 1u32 | 2u32 | 3u32), "base_observation_model");',
        b'\nassert!(matches!(before, 0u32 | 1u32 | 2u32 | 3u32), "base_observation_model");',
    )
    assert _result(raw, mutated_harness)["kind"] == "unknown"


def test_mutated_omission_without_counter_reconciliation_is_unknown() -> None:
    raw, harness = _fixture("preservation")
    exported = json.loads(raw)
    result = exported["verification_results"]["results"][0]
    result["checks"] = result["checks"][1:]
    for index, check in enumerate(result["checks"], start=1):
        check["id"] = index
    assert _result(json.dumps(exported).encode(), harness)["kind"] == "unknown"


def test_constructed_self_consistent_export_does_not_claim_source_proof() -> None:
    raw, harness = _fixture("preservation")
    exported = json.loads(raw)
    result = exported["verification_results"]["results"][0]
    result["checks"] = result["checks"][2:]
    for index, check in enumerate(result["checks"], start=1):
        check["id"] = index
    details = exported["property_details"][0]["property_details"]
    details.update(total_properties=4, passed=4)
    result_json = _result(json.dumps(exported).encode(), harness)
    assert result_json == {
        "kind": "verified",
        "reason": "all_qualified_properties_succeeded",
        "source_proof": False,
        "approval_authority": False,
    }


def test_mutated_exit_codes_cannot_change_transcript_classification() -> None:
    success, harness = _fixture("preservation")
    rejection, _ = _fixture("regression")
    assert _result(success, harness, 1)["kind"] == "unknown"
    assert _result(rejection, harness, 0)["kind"] == "unknown"
    assert _result(success, harness, True)["kind"] == "unknown"


def test_constructed_later_unreachable_assertion_keeps_named_rejection() -> None:
    raw, harness = _fixture("regression")
    exported = json.loads(raw)
    checks = exported["verification_results"]["results"][0]["checks"]
    standing = next(
        check
        for check in checks
        if check["description"] == '"candidate_standing_intent"'
    )
    relation = next(
        check for check in checks if check["description"] == '"relational_policy"'
    )
    standing["status"] = "Failure"
    relation["status"] = "Unreachable"
    details = exported["property_details"][0]["property_details"]
    details.update(passed=4, failed=1, unreachable=1)
    assert _result(json.dumps(exported).encode(), harness, 1) == {
        "kind": "proof_rejected",
        "reason": "named_candidate_standing_intent_assertion_failed",
        "source_proof": False,
        "approval_authority": False,
    }
