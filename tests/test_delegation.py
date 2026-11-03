from __future__ import annotations

from copy import deepcopy

import pytest

from vkit.certification import (
    ChangeCertificationRequest,
    ComparisonKind,
    DelegationBasis,
    EntailmentKind,
    IntentRevision,
    NearMiss,
    check_delegation,
    compare_contracts,
    intent_entails_claim,
)
from vkit.schemas import SchemaValidationError, validate


def domain(**changes):
    value = {
        "binding": "ledger::reserve",
        "inputs": ["x", "y"],
        "observations": ["zero", "one", "two"],
        "codec_sha256": "a" * 64,
        "observation_sha256": "b" * 64,
        "environment_sha256": "c" * 64,
    }
    value.update(changes)
    return value


def contract(outputs, *, admissible=("x",), domain_raw=None):
    return {
        "domain": domain_raw or domain(),
        "admissible_inputs": list(admissible),
        "allowed_outputs": [
            {"input": token, "outputs": values} for token, values in outputs.items()
        ],
    }


def finite(raw):
    request = {
        "schema_version": 1,
        "base_oid": "1" * 40,
        "candidate_oid": "2" * 40,
        "intent_sha256": "d" * 64,
        "claim": {"kind": "finite_contract", "contract": raw},
    }
    return ChangeCertificationRequest.from_json(request).claim


def intent(value):
    return IntentRevision("requirements.md#reserve", "Reserve within policy.", value, ())


def basis_for(intent_value, baseline, assumptions):
    return DelegationBasis(intent_value.digest, baseline, assumptions)


def accepted_case():
    accepted_intent = intent(finite(contract({"x": ["zero", "one"]})))
    baseline = finite(contract({"x": ["zero", "one", "two"]}))
    assumptions = finite(contract({"x": ["zero"]}))
    claim = finite(contract({"x": ["zero"]}))
    basis = basis_for(accepted_intent, baseline, assumptions)
    return accepted_intent, basis, claim, NearMiss("x", "two")


def test_delegation_accepts_claim_entailed_by_target_assumptions_and_strict_over_baseline():
    accepted_intent, basis, claim, near_miss = accepted_case()

    result = check_delegation(accepted_intent, basis, claim, near_miss)

    assert result.basis is basis
    assert result.claim is claim
    assert result.near_miss == NearMiss("x", "two")
    assert result.strictness_witness == ("x", "one")
    serialized = result.to_json()
    assert serialized["scope"] == "delegation_derivation"
    assert serialized["basis"] == basis.to_json()
    assert serialized["claim"] == claim.to_json()
    assert serialized["basis_sha256"] == basis.digest
    assert serialized["claim_sha256"] == claim.digest


def test_delegation_accepts_claim_when_intent_alone_already_entails_it():
    accepted_intent = intent(finite(contract({"x": ["zero"]})))
    baseline = finite(contract({"x": ["zero", "one"]}))
    assumptions = finite(contract({"x": ["zero", "one"]}))
    claim = finite(contract({"x": ["zero"]}))

    result = check_delegation(
        accepted_intent, basis_for(accepted_intent, baseline, assumptions),
        claim, NearMiss("x", "one"),
    )

    assert result.strictness_witness == ("x", "one")
    assert intent_entails_claim(accepted_intent.contract, claim).kind is EntailmentKind.ENTAILED
    assert compare_contracts(baseline, claim).kind is ComparisonKind.STRENGTHENS


def test_assumptions_can_narrow_intent_before_entailment_check():
    accepted_intent, basis, claim, near_miss = accepted_case()

    assert intent_entails_claim(accepted_intent.contract, claim).kind is EntailmentKind.NOT_ENTAILED
    assert check_delegation(accepted_intent, basis, claim, near_miss).claim == claim


def test_delegation_rejects_a_claim_equivalent_to_the_baseline():
    accepted_intent = intent(finite(contract({"x": ["zero", "one"]})))
    baseline = finite(contract({"x": ["zero", "one"]}))
    assumptions = finite(contract({"x": ["zero", "one"]}))

    with pytest.raises(ValueError, match="strictly strengthen"):
        check_delegation(
            accepted_intent, basis_for(accepted_intent, baseline, assumptions),
            baseline, NearMiss("x", "two"),
        )


def test_delegation_rejects_claim_not_entailed_by_intent_and_assumptions():
    accepted_intent, basis, _, near_miss = accepted_case()
    claim = finite(contract({"x": ["two"]}))

    with pytest.raises(ValueError, match="do not entail claim"):
        check_delegation(accepted_intent, basis, claim, near_miss)


def test_delegation_rejects_a_claim_that_excludes_a_legal_input():
    accepted_intent = intent(finite(contract(
        {"x": ["zero"], "y": ["zero"]}, admissible=("x", "y")
    )))
    baseline = finite(contract({"x": ["zero"], "y": ["zero", "one"]},
                               admissible=("x", "y")))
    assumptions = finite(contract({"x": ["zero"], "y": ["zero"]},
                                  admissible=("x", "y")))
    claim = finite(contract({"x": ["zero"]}))

    with pytest.raises(ValueError, match="same legal input set"):
        check_delegation(
            accepted_intent, basis_for(accepted_intent, baseline, assumptions),
            claim, NearMiss("x", "one"),
        )


def test_delegation_rejects_a_different_finite_domain():
    accepted_intent, basis, _, near_miss = accepted_case()
    claim = finite(contract({"x": ["zero"]}, domain_raw=domain(codec_sha256="e" * 64)))

    with pytest.raises(ValueError, match="different finite domains"):
        check_delegation(accepted_intent, basis, claim, near_miss)


def test_delegation_rejects_empty_intent_assumption_intersection():
    accepted_intent = intent(finite(contract({"x": ["zero"]})))
    baseline = finite(contract({"x": ["zero", "one"]}))
    assumptions = finite(contract({"x": ["one"]}))
    claim = finite(contract({"x": ["zero"]}))

    with pytest.raises(ValueError, match="empty output intersection"):
        check_delegation(
            accepted_intent, basis_for(accepted_intent, baseline, assumptions),
            claim, NearMiss("x", "one"),
        )


def test_delegation_rejects_basis_bound_to_another_intent_digest():
    accepted_intent, basis, claim, near_miss = accepted_case()
    wrong_basis = DelegationBasis("e" * 64, basis.baseline, basis.assumptions)

    with pytest.raises(ValueError, match="different accepted intent"):
        check_delegation(accepted_intent, wrong_basis, claim, near_miss)


@pytest.mark.parametrize(
    ("near_miss", "message"),
    [
        (NearMiss("y", "two"), "input is not legal"),
        (NearMiss("x", "unknown"), "outside the observation domain"),
        (NearMiss("x", "zero"), "allowed by the accepted intent"),
        (NearMiss("x", "one"), "allowed by the accepted intent"),
    ],
)
def test_delegation_rejects_invalid_near_miss(near_miss, message):
    accepted_intent, basis, claim, _ = accepted_case()

    with pytest.raises(ValueError, match=message):
        check_delegation(accepted_intent, basis, claim, near_miss)


def test_delegation_rejects_near_miss_output_not_allowed_by_baseline():
    accepted_intent = intent(finite(contract({"x": ["zero"]})))
    baseline = finite(contract({"x": ["zero", "one"]}))
    assumptions = finite(contract({"x": ["zero"]}))
    claim = finite(contract({"x": ["zero"]}))

    with pytest.raises(ValueError, match="not allowed by the baseline"):
        check_delegation(
            accepted_intent, basis_for(accepted_intent, baseline, assumptions),
            claim, NearMiss("x", "two"),
        )


def test_delegation_basis_schema_round_trip_is_canonical_and_closed():
    accepted_intent, basis, _, _ = accepted_case()
    raw = basis.to_json()

    parsed = DelegationBasis.from_json(raw)

    assert parsed.intent_sha256 == accepted_intent.digest
    assert parsed.to_json() == raw
    assert parsed.digest == basis.digest
    raw_with_extra = deepcopy(raw)
    raw_with_extra["accepted_by"] = "candidate"
    with pytest.raises(SchemaValidationError):
        DelegationBasis.from_json(raw_with_extra)


def test_change_request_optional_delegation_round_trips_without_changing_legacy_identity():
    accepted_intent, basis, claim, near_miss = accepted_case()
    raw = {
        "schema_version": 1,
        "base_oid": "1" * 40,
        "candidate_oid": "2" * 40,
        "intent_sha256": accepted_intent.digest,
        "claim": {"kind": "finite_contract", "contract": claim.to_json()},
    }
    legacy = ChangeCertificationRequest.from_json(raw)
    assert legacy.to_json() == raw

    raw["delegation"] = {
        "basis_sha256": basis.digest,
        "near_miss": near_miss.to_json(),
    }
    parsed = ChangeCertificationRequest.from_json(raw)

    assert parsed.delegation is not None
    assert parsed.delegation.to_json() == raw["delegation"]
    assert parsed.to_json() == raw
    raw["delegation"]["candidate_accepted"] = True
    with pytest.raises(SchemaValidationError):
        ChangeCertificationRequest.from_json(raw)


def test_accepted_basis_registry_schema_is_closed_and_uses_digests():
    accepted = {"schema_version": 1, "accepted": ["d" * 64]}

    validate("accepted claim bases", "accepted-claim-bases.v1.json", accepted)

    with pytest.raises(SchemaValidationError):
        validate("accepted claim bases", "accepted-claim-bases.v1.json",
                 {**accepted, "candidate_claim": True})
