from __future__ import annotations

import functools
import itertools
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from vkit.change.certification import (
    ComparisonKind,
    DelegationBasis,
    DelegationDerivation,
    EntailmentKind,
    FiniteContract,
    FiniteDomain,
    IntentRevision,
    NearMiss,
    OutputRow,
    SemanticArtifact,
    check_delegation,
    compare_contracts,
    intent_entails_claim,
)

INPUTS: tuple[str, ...] = ("x0", "x1")
OBSERVATIONS: tuple[str, ...] = ("y0", "y1")

_CODEC_SHA = "1" * 64
_OBS_SHA = "2" * 64
_ENV_SHA = "3" * 64
_BINDING_SHA = "4" * 64
_ALT_ENV_SHA = "5" * 64

PRIMARY_DOMAIN = FiniteDomain(
    binding="formal.delegation.2x2",
    inputs=INPUTS,
    observations=OBSERVATIONS,
    codec_sha256=_CODEC_SHA,
    observation_sha256=_OBS_SHA,
    environment_sha256=_ENV_SHA,
)

ALT_DOMAIN = FiniteDomain(
    binding="formal.delegation.2x2.alt",
    inputs=INPUTS,
    observations=OBSERVATIONS,
    codec_sha256=_CODEC_SHA,
    observation_sha256=_OBS_SHA,
    environment_sha256=_ALT_ENV_SHA,
)

_ARTIFACTS: tuple[SemanticArtifact, ...] = (
    SemanticArtifact("binding", "formal/binding.json", _BINDING_SHA),
    SemanticArtifact("codec", "formal/codec.json", _CODEC_SHA),
    SemanticArtifact("observation", "formal/observation.json", _OBS_SHA),
    SemanticArtifact("environment", "formal/environment.json", _ENV_SHA),
)


class RejectionReason(StrEnum):
    INTENT_DIGEST_MISMATCH = "delegation basis names a different accepted intent"
    DOMAIN_MISMATCH = "delegation contracts use different finite domains"
    EMPTY_LEGAL_INPUTS = "delegation requires a nonempty legal input set"
    LEGAL_INPUTS_MISMATCH = "delegation contracts must preserve the same legal input set"
    EMPTY_INTERSECTION = "empty output intersection"
    NOT_ENTAILED = "accepted intent and assumptions do not entail claim"
    NOT_STRICT_STRENGTHENING = (
        "claim must strictly strengthen the baseline without changing legal inputs"
    )
    NEAR_MISS_ILLEGAL_INPUT = "near-miss input is not legal"
    NEAR_MISS_OUT_OF_DOMAIN = "near-miss output is outside the observation domain"
    NEAR_MISS_NOT_IN_BASELINE = "near-miss output is not allowed by the baseline"
    NEAR_MISS_IN_INTENT = "near-miss output is allowed by the accepted intent"
    NEAR_MISS_IN_CLAIM = "near-miss output is allowed by the proposed claim"


@dataclass(frozen=True)
class OracleOutcome:
    accepted: bool
    reason: RejectionReason | None
    strictness_witness: tuple[str, str] | None


@dataclass(frozen=True)
class DelegationCase:
    intent: IntentRevision
    basis: DelegationBasis
    claim: FiniteContract
    near_miss: NearMiss
    suite: Literal["exhaustive_2x2", "exhaustive_1x2", "boundary"]


CheckerFn = Callable[
    [IntentRevision, DelegationBasis, FiniteContract, NearMiss],
    DelegationDerivation,
]


@dataclass(frozen=True)
class MutantSpec:
    name: str
    checker: CheckerFn
    expected_mismatch_kind: Literal["false_accept", "false_reject", "wrong_rejection_reason"]


@functools.lru_cache(maxsize=None)
def _cached_domain_digest(domain: FiniteDomain) -> str:
    return domain.digest


@functools.lru_cache(maxsize=None)
def _cached_intent_digest(intent: IntentRevision) -> str:
    return intent.digest


def _nonempty_subsets(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    subsets: list[tuple[str, ...]] = []
    for size in range(len(tokens), 0, -1):
        subsets.extend(itertools.combinations(tokens, size))
    return tuple(subsets)


@functools.lru_cache(maxsize=None)
def _contract_map(contract: FiniteContract) -> dict[str, frozenset[str]]:
    return {row.input: frozenset(row.outputs) for row in contract.rows}


def _make_contract(
    domain: FiniteDomain,
    admissible_inputs: tuple[str, ...],
    outputs_by_input: tuple[tuple[str, ...], ...],
) -> FiniteContract:
    rows = tuple(
        OutputRow(token, outputs)
        for token, outputs in zip(admissible_inputs, outputs_by_input, strict=True)
    )
    return FiniteContract(domain, admissible_inputs, rows)


def _make_intent(contract: FiniteContract) -> IntentRevision:
    return IntentRevision(
        requirement_source="formal/checks/check_delegation_algebra.py",
        requirement_statement="exhaustive 2x2 delegation algebra check",
        contract=contract,
        artifacts=_ARTIFACTS,
    )


def enumerate_contracts_for_inputs(
    domain: FiniteDomain,
    admissible_inputs: tuple[str, ...],
) -> tuple[FiniteContract, ...]:
    if not admissible_inputs:
        return (FiniteContract(domain, (), ()),)
    output_choices = _nonempty_subsets(domain.observations)
    return tuple(
        _make_contract(domain, admissible_inputs, combo)
        for combo in itertools.product(output_choices, repeat=len(admissible_inputs))
    )


def enumerate_all_contracts(domain: FiniteDomain) -> tuple[FiniteContract, ...]:
    contracts: list[FiniteContract] = []
    for size in range(len(domain.inputs) + 1):
        for input_subset in itertools.combinations(domain.inputs, size):
            contracts.extend(enumerate_contracts_for_inputs(domain, input_subset))
    return tuple(contracts)


def oracle_compare_contracts(old: FiniteContract, new: FiniteContract) -> ComparisonKind:
    if _cached_domain_digest(old.domain) != _cached_domain_digest(new.domain):
        return ComparisonKind.UNKNOWN
    old_inputs = frozenset(old.admissible_inputs)
    new_inputs = frozenset(new.admissible_inputs)
    if not old_inputs or not new_inputs:
        return ComparisonKind.VACUOUS
    old_map = _contract_map(old)
    new_map = _contract_map(new)
    old_to_new = old_inputs <= new_inputs and all(
        new_map[token] <= old_map[token] for token in old_inputs
    )
    new_to_old = new_inputs <= old_inputs and all(
        old_map[token] <= new_map[token] for token in new_inputs
    )
    if old_to_new and new_to_old:
        return ComparisonKind.EQUIVALENT
    if old_to_new:
        return ComparisonKind.STRENGTHENS
    if new_to_old:
        return ComparisonKind.WEAKENS
    return ComparisonKind.INCOMPARABLE


def oracle_intent_entails_claim(intent: FiniteContract, claim: FiniteContract) -> EntailmentKind:
    if _cached_domain_digest(intent.domain) != _cached_domain_digest(claim.domain):
        return EntailmentKind.UNKNOWN
    intent_inputs = frozenset(intent.admissible_inputs)
    if not intent_inputs:
        return EntailmentKind.VACUOUS
    claim_inputs = frozenset(claim.admissible_inputs)
    if not (intent_inputs <= claim_inputs):
        return EntailmentKind.NOT_ENTAILED
    intent_map = _contract_map(intent)
    claim_map = _contract_map(claim)
    if all(intent_map[token] <= claim_map[token] for token in intent_inputs):
        return EntailmentKind.ENTAILED
    return EntailmentKind.NOT_ENTAILED


def oracle_check_delegation(
    intent: IntentRevision,
    basis: DelegationBasis,
    claim: FiniteContract,
    near_miss: NearMiss,
) -> OracleOutcome:
    if basis.intent_sha256 != _cached_intent_digest(intent):
        return OracleOutcome(False, RejectionReason.INTENT_DIGEST_MISMATCH, None)

    intent_contract = intent.contract
    baseline = basis.baseline
    assumptions = basis.assumptions
    expected_domain = _cached_domain_digest(intent_contract.domain)
    if any(
        _cached_domain_digest(contract.domain) != expected_domain
        for contract in (baseline, assumptions, claim)
    ):
        return OracleOutcome(False, RejectionReason.DOMAIN_MISMATCH, None)

    legal_inputs = frozenset(intent_contract.admissible_inputs)
    if not legal_inputs:
        return OracleOutcome(False, RejectionReason.EMPTY_LEGAL_INPUTS, None)
    if any(
        frozenset(contract.admissible_inputs) != legal_inputs
        for contract in (baseline, assumptions, claim)
    ):
        return OracleOutcome(False, RejectionReason.LEGAL_INPUTS_MISMATCH, None)

    intent_map = _contract_map(intent_contract)
    baseline_map = _contract_map(baseline)
    assumptions_map = _contract_map(assumptions)
    claim_map = _contract_map(claim)

    constrained: dict[str, frozenset[str]] = {}
    for token in sorted(legal_inputs):
        intersection = intent_map[token] & assumptions_map[token]
        if not intersection:
            return OracleOutcome(False, RejectionReason.EMPTY_INTERSECTION, None)
        constrained[token] = intersection

    if not all(constrained[token] <= claim_map[token] for token in legal_inputs):
        return OracleOutcome(False, RejectionReason.NOT_ENTAILED, None)

    claim_subset_baseline = all(claim_map[token] <= baseline_map[token] for token in legal_inputs)
    strict_differences = sorted(
        (token, output)
        for token in legal_inputs
        for output in (baseline_map[token] - claim_map[token])
    )
    if not claim_subset_baseline or not strict_differences:
        return OracleOutcome(False, RejectionReason.NOT_STRICT_STRENGTHENING, None)

    if near_miss.input not in legal_inputs:
        return OracleOutcome(False, RejectionReason.NEAR_MISS_ILLEGAL_INPUT, None)
    if near_miss.output not in frozenset(intent_contract.domain.observations):
        return OracleOutcome(False, RejectionReason.NEAR_MISS_OUT_OF_DOMAIN, None)
    if near_miss.output not in baseline_map[near_miss.input]:
        return OracleOutcome(False, RejectionReason.NEAR_MISS_NOT_IN_BASELINE, None)
    if near_miss.output in intent_map[near_miss.input]:
        return OracleOutcome(False, RejectionReason.NEAR_MISS_IN_INTENT, None)
    if near_miss.output in claim_map[near_miss.input]:
        return OracleOutcome(False, RejectionReason.NEAR_MISS_IN_CLAIM, None)

    return OracleOutcome(True, None, strict_differences[0])


def _describe_case(case: DelegationCase) -> dict[str, Any]:
    def _rows(contract: FiniteContract) -> dict[str, list[str]]:
        return {
            row.input: sorted(row.outputs)
            for row in sorted(contract.rows, key=lambda r: r.input)
        }

    return {
        "suite": case.suite,
        "intent": _rows(case.intent.contract),
        "baseline": _rows(case.basis.baseline),
        "assumptions": _rows(case.basis.assumptions),
        "claim": _rows(case.claim),
        "near_miss": case.near_miss.to_json(),
    }


def enumerate_exhaustive_2x2_cases() -> list[DelegationCase]:
    contracts = enumerate_contracts_for_inputs(PRIMARY_DOMAIN, INPUTS)
    intents = tuple((contract, _make_intent(contract)) for contract in contracts)
    near_misses = tuple(
        NearMiss(input_token, output_token)
        for input_token in INPUTS
        for output_token in OBSERVATIONS
    )
    cases: list[DelegationCase] = []
    for _, intent in intents:
        intent_digest = _cached_intent_digest(intent)
        for baseline in contracts:
            for assumptions in contracts:
                basis = DelegationBasis(intent_digest, baseline, assumptions)
                for claim in contracts:
                    for near_miss in near_misses:
                        cases.append(
                            DelegationCase(intent, basis, claim, near_miss, "exhaustive_2x2")
                        )
    return cases


def enumerate_exhaustive_1x2_cases() -> list[DelegationCase]:
    single_input = ("x0",)
    contracts = enumerate_contracts_for_inputs(PRIMARY_DOMAIN, single_input)
    intents = tuple((contract, _make_intent(contract)) for contract in contracts)
    near_misses = tuple(NearMiss("x0", output_token) for output_token in OBSERVATIONS)
    cases: list[DelegationCase] = []
    for _, intent in intents:
        intent_digest = _cached_intent_digest(intent)
        for baseline in contracts:
            for assumptions in contracts:
                basis = DelegationBasis(intent_digest, baseline, assumptions)
                for claim in contracts:
                    for near_miss in near_misses:
                        cases.append(
                            DelegationCase(intent, basis, claim, near_miss, "exhaustive_1x2")
                        )
    return cases


def enumerate_boundary_cases() -> list[DelegationCase]:
    valid_intent_contract = _make_contract(PRIMARY_DOMAIN, INPUTS, (("y0",), ("y0", "y1")))
    valid_baseline = _make_contract(PRIMARY_DOMAIN, INPUTS, (("y0", "y1"), ("y0", "y1")))
    valid_assumptions = _make_contract(PRIMARY_DOMAIN, INPUTS, (("y0", "y1"), ("y0", "y1")))
    valid_claim = _make_contract(PRIMARY_DOMAIN, INPUTS, (("y0",), ("y0", "y1")))
    valid_intent = _make_intent(valid_intent_contract)
    valid_digest = _cached_intent_digest(valid_intent)
    valid_basis = DelegationBasis(valid_digest, valid_baseline, valid_assumptions)
    valid_near_miss = NearMiss("x0", "y1")

    alt_contract = _make_contract(ALT_DOMAIN, INPUTS, (("y0", "y1"), ("y0", "y1")))
    empty_contract = FiniteContract(PRIMARY_DOMAIN, (), ())
    empty_intent = _make_intent(empty_contract)
    single_x0_contract = _make_contract(PRIMARY_DOMAIN, ("x0",), (("y0",),))
    single_x1_contract = _make_contract(PRIMARY_DOMAIN, ("x1",), (("y0",),))

    return [
        # Wrong intent digest in DelegationBasis
        DelegationCase(
            valid_intent,
            DelegationBasis("0" * 64, valid_baseline, valid_assumptions),
            valid_claim,
            valid_near_miss,
            "boundary",
        ),
        # Domain mismatches in baseline, assumptions, claim
        DelegationCase(
            valid_intent,
            DelegationBasis(valid_digest, alt_contract, valid_assumptions),
            valid_claim,
            valid_near_miss,
            "boundary",
        ),
        DelegationCase(
            valid_intent,
            DelegationBasis(valid_digest, valid_baseline, alt_contract),
            valid_claim,
            valid_near_miss,
            "boundary",
        ),
        DelegationCase(
            valid_intent,
            valid_basis,
            alt_contract,
            valid_near_miss,
            "boundary",
        ),
        # Vacuous (empty) legal input set
        DelegationCase(
            empty_intent,
            DelegationBasis(_cached_intent_digest(empty_intent), empty_contract, empty_contract),
            empty_contract,
            valid_near_miss,
            "boundary",
        ),
        # Legal input set mismatches in baseline, assumptions, claim
        DelegationCase(
            valid_intent,
            DelegationBasis(valid_digest, single_x0_contract, valid_assumptions),
            valid_claim,
            valid_near_miss,
            "boundary",
        ),
        DelegationCase(
            valid_intent,
            DelegationBasis(valid_digest, valid_baseline, single_x1_contract),
            valid_claim,
            valid_near_miss,
            "boundary",
        ),
        DelegationCase(
            valid_intent,
            valid_basis,
            single_x0_contract,
            valid_near_miss,
            "boundary",
        ),
        # Illegal near-miss input (outside legal inputs and outside domain)
        DelegationCase(
            valid_intent,
            valid_basis,
            valid_claim,
            NearMiss("x_outside", "y1"),
            "boundary",
        ),
        DelegationCase(
            _make_intent(single_x0_contract),
            DelegationBasis(
                _cached_intent_digest(_make_intent(single_x0_contract)),
                _make_contract(PRIMARY_DOMAIN, ("x0",), (("y0", "y1"),)),
                _make_contract(PRIMARY_DOMAIN, ("x0",), (("y0", "y1"),)),
            ),
            single_x0_contract,
            NearMiss("x1", "y1"),
            "boundary",
        ),
        # Out-of-domain near-miss output
        DelegationCase(
            valid_intent,
            valid_basis,
            valid_claim,
            NearMiss("x0", "y_outside"),
            "boundary",
        ),
    ]


def verify_delegation_checker(
    checker: CheckerFn,
    cases: Iterable[DelegationCase],
    *,
    stop_on_mismatch_kind: str | None = None,
) -> dict[str, Any]:
    checked = 0
    accepted_count = 0
    rejection_counts: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []

    for case in cases:
        checked += 1
        expected = oracle_check_delegation(
            case.intent, case.basis, case.claim, case.near_miss
        )
        mismatch: dict[str, Any] | None = None
        try:
            actual = checker(case.intent, case.basis, case.claim, case.near_miss)
        except ValueError as exc:
            error_message = str(exc)
            if expected.accepted:
                mismatch = {
                    "kind": "false_reject",
                    "expected": "accepted",
                    "actual": error_message,
                    "case": _describe_case(case),
                }
            else:
                assert expected.reason is not None
                if expected.reason.value not in error_message:
                    mismatch = {
                        "kind": "wrong_rejection_reason",
                        "expected": expected.reason.value,
                        "actual": error_message,
                        "case": _describe_case(case),
                    }
                else:
                    rejection_counts[expected.reason.name] += 1
        else:
            if not expected.accepted:
                assert expected.reason is not None
                mismatch = {
                    "kind": "false_accept",
                    "expected": expected.reason.value,
                    "actual": {
                        "strictness_witness": list(actual.strictness_witness),
                    },
                    "case": _describe_case(case),
                }
            elif (
                actual.basis != case.basis
                or actual.claim != case.claim
                or actual.near_miss != case.near_miss
                or actual.strictness_witness != expected.strictness_witness
            ):
                mismatch = {
                    "kind": "wrong_derivation_payload",
                    "expected_witness": list(expected.strictness_witness or ()),
                    "actual_witness": list(actual.strictness_witness),
                    "case": _describe_case(case),
                }
            else:
                accepted_count += 1

        if mismatch is not None:
            if stop_on_mismatch_kind is None:
                mismatches.append(mismatch)
            elif mismatch["kind"] == stop_on_mismatch_kind:
                mismatches.append(mismatch)
                break

    return {
        "checked": checked,
        "accepted": accepted_count,
        "rejected": sum(rejection_counts.values()),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "mismatches": mismatches,
        "passed": len(mismatches) == 0,
    }


def verify_aux_relations() -> dict[str, Any]:
    all_contracts = enumerate_all_contracts(PRIMARY_DOMAIN)
    alt_contract = _make_contract(ALT_DOMAIN, INPUTS, (("y0",), ("y1",)))

    compare_counts: Counter[str] = Counter()
    compare_mismatches: list[dict[str, Any]] = []
    for old in all_contracts:
        for new in all_contracts:
            expected = oracle_compare_contracts(old, new)
            actual = compare_contracts(old, new).kind
            if actual != expected:
                compare_mismatches.append({"expected": expected.value, "actual": actual.value})
            else:
                compare_counts[actual.value] += 1
    actual_unknown_cmp = compare_contracts(all_contracts[-1], alt_contract).kind
    expected_unknown_cmp = oracle_compare_contracts(all_contracts[-1], alt_contract)
    if actual_unknown_cmp != expected_unknown_cmp:
        compare_mismatches.append({
            "expected": expected_unknown_cmp.value,
            "actual": actual_unknown_cmp.value,
        })
    else:
        compare_counts[actual_unknown_cmp.value] += 1

    entail_counts: Counter[str] = Counter()
    entail_mismatches: list[dict[str, Any]] = []
    for intent in all_contracts:
        for claim in all_contracts:
            expected = oracle_intent_entails_claim(intent, claim)
            actual = intent_entails_claim(intent, claim).kind
            if actual != expected:
                entail_mismatches.append({"expected": expected.value, "actual": actual.value})
            else:
                entail_counts[actual.value] += 1
    actual_unknown_ent = intent_entails_claim(all_contracts[-1], alt_contract).kind
    expected_unknown_ent = oracle_intent_entails_claim(all_contracts[-1], alt_contract)
    if actual_unknown_ent != expected_unknown_ent:
        entail_mismatches.append({
            "expected": expected_unknown_ent.value,
            "actual": actual_unknown_ent.value,
        })
    else:
        entail_counts[actual_unknown_ent.value] += 1

    return {
        "compare_contracts_checked": len(all_contracts) ** 2 + 1,
        "compare_contracts_counts": dict(sorted(compare_counts.items())),
        "compare_contracts_mismatches": compare_mismatches,
        "intent_entails_claim_checked": len(all_contracts) ** 2 + 1,
        "intent_entails_claim_counts": dict(sorted(entail_counts.items())),
        "intent_entails_claim_mismatches": entail_mismatches,
        "passed": not compare_mismatches and not entail_mismatches,
    }


def _check_delegation_variant(
    intent: IntentRevision,
    basis: DelegationBasis,
    claim: FiniteContract,
    near_miss: NearMiss,
    *,
    allow_equivalent: bool = False,
    allow_incomparable: bool = False,
    check_near_miss_intent: bool = True,
    check_near_miss_claim: bool = True,
    intersect_assumptions: bool = True,
) -> DelegationDerivation:
    if basis.intent_sha256 != intent.digest:
        raise ValueError("delegation basis names a different accepted intent")

    baseline = basis.baseline
    assumptions = basis.assumptions
    contracts = (intent.contract, baseline, assumptions, claim)
    if any(c.domain.digest != intent.contract.domain.digest for c in contracts[1:]):
        raise ValueError("delegation contracts use different finite domains")

    legal_inputs = set(intent.contract.admissible_inputs)
    if not legal_inputs:
        raise ValueError("delegation requires a nonempty legal input set")
    if any(set(c.admissible_inputs) != legal_inputs for c in contracts[1:]):
        raise ValueError("delegation contracts must preserve the same legal input set")

    if intersect_assumptions:
        derived_rows = []
        for token in sorted(legal_inputs):
            outputs = intent.contract.outputs_for(token) & assumptions.outputs_for(token)
            if not outputs:
                raise ValueError(
                    f"intent and assumptions have an empty output intersection for {token!r}"
                )
            derived_rows.append(OutputRow(token, tuple(sorted(outputs))))
        entailment_source = FiniteContract(
            intent.contract.domain, tuple(sorted(legal_inputs)), tuple(derived_rows)
        )
    else:
        entailment_source = intent.contract

    if intent_entails_claim(entailment_source, claim).kind is not EntailmentKind.ENTAILED:
        raise ValueError("accepted intent and assumptions do not entail claim")

    comparison = compare_contracts(baseline, claim)
    allowed_kinds = {ComparisonKind.STRENGTHENS}
    if allow_equivalent:
        allowed_kinds.add(ComparisonKind.EQUIVALENT)
    if allow_incomparable:
        allowed_kinds.add(ComparisonKind.INCOMPARABLE)
    if comparison.kind not in allowed_kinds:
        raise ValueError("claim must strictly strengthen the baseline without changing legal inputs")

    witness = comparison.new_to_old.output_witness if comparison.new_to_old else None
    if witness is None:
        if allow_equivalent:
            witness = (near_miss.input, near_miss.output)
        else:
            raise ValueError("strict strengthening has no output witness")

    if near_miss.input not in legal_inputs:
        raise ValueError("near-miss input is not legal")
    if near_miss.output not in intent.contract.domain.observations:
        raise ValueError("near-miss output is outside the observation domain")
    if near_miss.output not in baseline.outputs_for(near_miss.input):
        raise ValueError("near-miss output is not allowed by the baseline")
    if check_near_miss_intent and near_miss.output in intent.contract.outputs_for(near_miss.input):
        raise ValueError("near-miss output is allowed by the accepted intent")
    if check_near_miss_claim and near_miss.output in claim.outputs_for(near_miss.input):
        raise ValueError("near-miss output is allowed by the proposed claim")

    return DelegationDerivation(basis, claim, near_miss, witness)


NEGATIVE_CONTROL_MUTANTS: tuple[MutantSpec, ...] = (
    MutantSpec(
        name="accept_nonstrict_strengthening",
        checker=functools.partial(_check_delegation_variant, allow_equivalent=True),
        expected_mismatch_kind="wrong_rejection_reason",
    ),
    MutantSpec(
        name="allow_incomparable_claim",
        checker=functools.partial(_check_delegation_variant, allow_incomparable=True),
        expected_mismatch_kind="false_accept",
    ),
    MutantSpec(
        name="ignore_near_miss_intent_overlap",
        checker=functools.partial(_check_delegation_variant, check_near_miss_intent=False),
        expected_mismatch_kind="false_accept",
    ),
    MutantSpec(
        name="ignore_near_miss_claim_overlap",
        checker=functools.partial(_check_delegation_variant, check_near_miss_claim=False),
        expected_mismatch_kind="false_accept",
    ),
    MutantSpec(
        name="ignore_assumptions_intersection",
        checker=functools.partial(_check_delegation_variant, intersect_assumptions=False),
        expected_mismatch_kind="false_reject",
    ),
)


def run() -> dict[str, Any]:
    started = time.perf_counter()

    exhaustive_2x2 = enumerate_exhaustive_2x2_cases()
    exhaustive_1x2 = enumerate_exhaustive_1x2_cases()
    boundary = enumerate_boundary_cases()
    all_cases = [*exhaustive_2x2, *exhaustive_1x2, *boundary]

    aux_report = verify_aux_relations()
    real_report = verify_delegation_checker(check_delegation, all_cases)

    mutant_reports: dict[str, dict[str, Any]] = {}
    all_mutants_caught = True
    for spec in NEGATIVE_CONTROL_MUTANTS:
        outcome = verify_delegation_checker(
            spec.checker,
            all_cases,
            stop_on_mismatch_kind=spec.expected_mismatch_kind,
        )
        caught = not outcome["passed"]
        if not caught:
            all_mutants_caught = False
        mutant_reports[spec.name] = {
            "caught": caught,
            "expected_mismatch_kind": spec.expected_mismatch_kind,
            "cases_before_catch": outcome["checked"],
            "counterexample": outcome["mismatches"][0] if outcome["mismatches"] else None,
        }

    elapsed_seconds = round(time.perf_counter() - started, 4)
    passed = aux_report["passed"] and real_report["passed"] and all_mutants_caught

    return {
        "target": "src/vkit/change/certification.py::check_delegation",
        "domain": {
            "inputs": list(INPUTS),
            "observations": list(OBSERVATIONS),
            "exhaustive_2x2_tuples": len(exhaustive_2x2),
            "exhaustive_1x2_tuples": len(exhaustive_1x2),
            "boundary_tuples": len(boundary),
            "total_delegation_tuples": len(all_cases),
        },
        "aux_relations": aux_report,
        "real_code": real_report,
        "negative_controls": {
            "all_caught": all_mutants_caught,
            "mutants": mutant_reports,
        },
        "elapsed_seconds": elapsed_seconds,
        "passed": passed,
    }


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
