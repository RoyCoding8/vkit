from __future__ import annotations

from copy import deepcopy

import pytest

from vkit.certification import (
    ComparisonKind,
    ChangeCertificationRequest,
    EntailmentKind,
    compare_contracts,
    intent_entails_claim,
)
from vkit.schemas import SchemaValidationError


def domain(**changes):
    value = {
        "binding": "ledger::reserve",
        "inputs": ["empty", "available"],
        "observations": ["rejected", "reserved", "unchanged"],
        "codec_sha256": "a" * 64,
        "observation_sha256": "b" * 64,
        "environment_sha256": "c" * 64,
    }
    value.update(changes)
    return value


def contract(admissible=("available",), outputs=None, domain_raw=None):
    if outputs is None:
        outputs = {"available": ["reserved", "unchanged"]}
    return {
        "domain": domain_raw or domain(),
        "admissible_inputs": list(admissible),
        "allowed_outputs": [
            {"input": token, "outputs": values} for token, values in outputs.items()
        ],
    }


def request(contract_raw=None):
    return {
        "schema_version": 1,
        "base_oid": "1" * 40,
        "candidate_oid": "2" * 40,
        "intent_sha256": "d" * 64,
        "claim": {
            "kind": "finite_contract",
            "contract": contract_raw or contract(),
        },
    }


def parse_contract(raw):
    return ChangeCertificationRequest.from_json(request(raw)).claim


def test_refinement_reports_strengthening_with_separate_pre_and_post_results():
    old = parse_contract(contract(outputs={"available": ["reserved", "unchanged"]}))
    new = parse_contract(contract(outputs={"available": ["reserved"]}))

    result = compare_contracts(old, new)

    assert result.kind is ComparisonKind.STRENGTHENS
    assert result.old_to_new.precondition_included is True
    assert result.old_to_new.postcondition_included is True
    assert result.new_to_old.precondition_included is True
    assert result.new_to_old.postcondition_included is False
    assert result.new_to_old.output_witness == ("available", "unchanged")


def test_refinement_reports_weakening_and_equivalence():
    narrow = parse_contract(contract(outputs={"available": ["reserved"]}))
    broad = parse_contract(contract(outputs={"available": ["reserved", "unchanged"]}))
    reordered = parse_contract(contract(outputs={"available": ["unchanged", "reserved"]}))

    assert compare_contracts(narrow, broad).kind is ComparisonKind.WEAKENS
    assert compare_contracts(broad, narrow).kind is ComparisonKind.STRENGTHENS
    assert compare_contracts(broad, reordered).kind is ComparisonKind.EQUIVALENT
    assert narrow.domain.digest == parse_contract(
        contract(domain_raw=domain(inputs=["available", "empty"],
                                   observations=["unchanged", "reserved", "rejected"]))
    ).domain.digest


def test_refinement_reports_incomparable_pre_and_post_failures():
    old = parse_contract(contract(admissible=("available",)))
    new = parse_contract(contract(
        admissible=("empty",),
        outputs={"empty": ["rejected"]},
    ))

    result = compare_contracts(old, new)

    assert result.kind is ComparisonKind.INCOMPARABLE
    assert result.old_to_new.precondition_included is False
    assert result.old_to_new.input_witness == "available"
    assert result.new_to_old.precondition_included is False
    assert result.new_to_old.input_witness == "empty"


def test_empty_legal_domain_is_vacuous():
    empty = parse_contract(contract(admissible=(), outputs={}))
    inhabited = parse_contract(contract())

    result = compare_contracts(empty, inhabited)

    assert result.kind is ComparisonKind.VACUOUS
    assert result.old_to_new.precondition_included is True
    assert result.new_to_old.precondition_included is False


@pytest.mark.parametrize("field,value", [
    ("codec_sha256", "e" * 64),
    ("observation_sha256", "e" * 64),
    ("environment_sha256", "e" * 64),
    ("binding", "ledger::release"),
    ("observations", ["rejected", "reserved", "unchanged", "extra"]),
])
def test_changed_domain_semantics_return_unknown(field, value):
    old = parse_contract(contract())
    changed = domain(**{field: value})
    new = parse_contract(contract(domain_raw=changed))

    result = compare_contracts(old, new)

    assert result.kind is ComparisonKind.UNKNOWN
    assert result.old_to_new is None
    assert result.new_to_old is None


def test_intent_entailment_rejects_a_claim_that_excludes_a_legal_output():
    intent = parse_contract(contract(outputs={"available": ["reserved", "unchanged"]}))
    overconstrained_claim = parse_contract(contract(outputs={"available": ["reserved"]}))

    result = intent_entails_claim(intent, overconstrained_claim)

    assert result.kind is EntailmentKind.NOT_ENTAILED
    assert result.inputs_included is True
    assert result.outputs_included is False
    assert result.output_witness == ("available", "unchanged")


def test_intent_entailment_fails_when_claim_excludes_an_input_with_same_outputs():
    intent = parse_contract(contract(admissible=("available", "empty"), outputs={
        "available": ["reserved"],
        "empty": ["rejected"],
    }))
    claim = parse_contract(contract(outputs={"available": ["reserved"]}))

    result = intent_entails_claim(intent, claim)

    assert result.kind is EntailmentKind.NOT_ENTAILED
    assert result.inputs_included is False
    assert result.outputs_included is True
    assert result.input_witness == "empty"


def test_intent_entailment_accepts_claim_with_extra_disallowed_outputs():
    intent = parse_contract(contract(outputs={"available": ["reserved"]}))
    broader_claim = parse_contract(contract(outputs={
        "available": ["reserved", "unchanged"],
    }))

    result = intent_entails_claim(intent, broader_claim)

    assert result.kind is EntailmentKind.ENTAILED
    assert result.inputs_included is True
    assert result.outputs_included is True


def test_intent_entailment_is_unknown_on_domain_mismatch():
    intent = parse_contract(contract())
    claim = parse_contract(contract(domain_raw=domain(codec_sha256="e" * 64)))

    result = intent_entails_claim(intent, claim)

    assert result.kind is EntailmentKind.UNKNOWN
    assert result.inputs_included is None
    assert result.outputs_included is None


def test_empty_intent_entailment_is_vacuous():
    intent = parse_contract(contract(admissible=(), outputs={}))
    claim = parse_contract(contract())

    assert intent_entails_claim(intent, claim).kind is EntailmentKind.VACUOUS


def test_change_request_parser_keeps_claimed_intent_digest_untrusted():
    parsed = ChangeCertificationRequest.from_json(request())

    assert parsed.base_oid == "1" * 40
    assert parsed.candidate_oid == "2" * 40
    assert parsed.declared_intent_sha256 == "d" * 64
    assert parsed.claim.admissible_inputs == ("available",)


def test_change_request_parser_accepts_a_64_character_object_format():
    raw = request()
    raw["base_oid"] = "1" * 64
    raw["candidate_oid"] = "2" * 64

    parsed = ChangeCertificationRequest.from_json(raw)

    assert (len(parsed.base_oid), len(parsed.candidate_oid)) == (64, 64)


def test_parser_rejects_incomplete_output_rows():
    raw = request(contract_raw=contract(outputs={}))

    with pytest.raises(ValueError, match="exactly one row"):
        ChangeCertificationRequest.from_json(raw)


@pytest.mark.parametrize("kind", ["verus_source", "expression", "unknown"])
def test_parser_rejects_unsupported_claim_discriminants(kind):
    raw = request()
    raw["claim"]["kind"] = kind

    with pytest.raises(SchemaValidationError):
        ChangeCertificationRequest.from_json(raw)


@pytest.mark.parametrize("field,value", [
    ("base_oid", "HEAD"),
    ("candidate_oid", "a" * 39),
])
def test_parser_requires_full_git_object_identities(field, value):
    raw = request()
    raw[field] = value

    with pytest.raises(SchemaValidationError):
        ChangeCertificationRequest.from_json(raw)


def test_parser_rejects_mixed_git_object_formats():
    raw = request()
    raw["candidate_oid"] = "2" * 64

    with pytest.raises(ValueError, match="same object format"):
        ChangeCertificationRequest.from_json(raw)


@pytest.mark.parametrize("field,value", [
    ("intent_sha256", "d" * 64 + "\n"),
    ("base_oid", "1" * 40 + "\n"),
    ("candidate_oid", "2" * 64 + "\n"),
])
def test_parser_rejects_trailing_newline_in_fixed_length_identity(field, value):
    raw = request()
    raw[field] = value

    with pytest.raises(SchemaValidationError):
        ChangeCertificationRequest.from_json(raw)


def test_parser_rejects_output_tokens_outside_domain_universe():
    raw = request(contract_raw=contract(outputs={"available": ["not-a-token"]}))

    with pytest.raises(ValueError, match="outside the domain"):
        ChangeCertificationRequest.from_json(raw)


def test_parser_rejects_duplicate_rows_and_duplicate_tokens():
    raw = request()
    raw["claim"]["contract"]["allowed_outputs"].append(
        deepcopy(raw["claim"]["contract"]["allowed_outputs"][0])
    )

    with pytest.raises(ValueError, match="duplicate input rows"):
        ChangeCertificationRequest.from_json(raw)
