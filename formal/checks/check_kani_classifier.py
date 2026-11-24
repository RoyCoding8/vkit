"""Exhaustive real-code formal checker for vkit.rust.kani_result.classify_kani_result."""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass
from typing import Callable

from vkit.rust.kani_result import KaniOutcome, classify_kani_result

STATUSES: tuple[str, ...] = (
    "Success",
    "Failure",
    "Unreachable",
    "Undetermined",
    "SolverError",
)
EXIT_CODES: tuple[int, ...] = (0, 1, 2)
HARNESS_NAME = "vkit_relational"
BASE_SOURCE_FILE = "base/base.rs"
CANDIDATE_SOURCE_FILE = "candidate/candidate.rs"

HARNESS_ASSERTIONS: tuple[str, ...] = (
    "base_observation_model",
    "candidate_observation_model",
    "candidate_standing_intent",
    "relational_policy",
)
TARGET_ASSERTIONS: frozenset[str] = frozenset(
    {"candidate_standing_intent", "relational_policy"}
)
OBSERVATION_ASSERTIONS: frozenset[str] = frozenset(
    {"base_observation_model", "candidate_observation_model"}
)

CANONICAL_HARNESS_SOURCE = (
    '#[path = "base/base.rs"] mod vkit_base;\n'
    '#[path = "candidate/candidate.rs"] mod vkit_candidate;\n'
    "#[kani::proof]\n"
    "fn vkit_relational() {\n"
    '    assert!(true, "base_observation_model");\n'
    '    assert!(true, "candidate_observation_model");\n'
    '    assert!(true, "candidate_standing_intent");\n'
    '    assert!(true, "relational_policy");\n'
    "}\n"
)
CANONICAL_HARNESS_BYTES = CANONICAL_HARNESS_SOURCE.encode("utf-8")
ASSERTION_LINES: dict[str, int] = {
    "base_observation_model": 5,
    "candidate_observation_model": 6,
    "candidate_standing_intent": 7,
    "relational_policy": 8,
}

ClassifierFn = Callable[..., KaniOutcome]


@dataclass(frozen=True, slots=True)
class TranscriptTuple:
    base_observation_model: str
    candidate_observation_model: str
    candidate_standing_intent: str
    relational_policy: str
    base_source: str
    candidate_source: str
    exit_code: int

    def assertion_statuses(self) -> dict[str, str]:
        return {
            "base_observation_model": self.base_observation_model,
            "candidate_observation_model": self.candidate_observation_model,
            "candidate_standing_intent": self.candidate_standing_intent,
            "relational_policy": self.relational_policy,
        }

    def all_statuses(self) -> tuple[str, ...]:
        return (
            self.base_source,
            self.candidate_source,
            self.base_observation_model,
            self.candidate_observation_model,
            self.candidate_standing_intent,
            self.relational_policy,
        )


@dataclass(frozen=True, slots=True)
class CounterMismatchCase:
    name: str
    baseline: str
    raw: bytes
    exit_code: int | bool
    expected_reason: str


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: str
    case: str
    expected: str
    actual: str


def build_transcript_doc(case: TranscriptTuple) -> dict[str, object]:
    """Construct a schema-valid, counter-consistent Kani JSON export for a tuple."""
    checks: list[dict[str, object]] = [
        {
            "id": 1,
            "category": "assertion",
            "status": case.base_source,
            "function": "vkit_base::step",
            "description": "attempt to add with overflow",
            "location": {"file": BASE_SOURCE_FILE, "line": "10", "column": "5"},
        },
        {
            "id": 2,
            "category": "assertion",
            "status": case.candidate_source,
            "function": "vkit_candidate::step",
            "description": "attempt to add with overflow",
            "location": {"file": CANDIDATE_SOURCE_FILE, "line": "10", "column": "5"},
        },
    ]
    for offset, name in enumerate(HARNESS_ASSERTIONS, start=3):
        checks.append(
            {
                "id": offset,
                "category": "assertion",
                "status": getattr(case, name),
                "function": HARNESS_NAME,
                "description": f'"{name}"',
                "location": {
                    "file": "harness.rs",
                    "line": str(ASSERTION_LINES[name]),
                    "column": "1",
                },
            }
        )

    statuses = case.all_statuses()
    counts = {
        "total_properties": len(checks),
        "passed": statuses.count("Success"),
        "failed": statuses.count("Failure"),
        "unreachable": statuses.count("Unreachable"),
        "undetermined": statuses.count("Undetermined"),
        "solver_error": statuses.count("SolverError"),
        "satisfied": 0,
        "unsatisfiable": 0,
        "covered": 0,
        "uncovered": 0,
    }
    has_non_success = counts["passed"] != len(checks)
    row_status = "Failure" if has_non_success else "Success"

    error_entry: dict[str, object] = {
        "harness_id": HARNESS_NAME,
        "has_errors": has_non_success,
    }
    if has_non_success:
        error_entry.update(
            {
                "error_type": "assertion_failure",
                "failed_properties_type": "PanicsOnly",
                "exit_status": "properties_failed",
            }
        )

    return {
        "metadata": {
            "version": "1.0",
            "kani_version": "0.68.0",
            "target": "x86_64-unknown-linux-gnu",
            "build_mode": "release",
        },
        "tools": {
            "kani": "0.68.0",
            "rustc": "rustc 1.100.0-nightly (8925ea358 2026-08-20)",
            "cbmc": "6.11.0 (cbmc-6.11.0)",
            "goto_cc": "gcc (goto-cc 6.11.0 (cbmc-6.11.0)) 15.2.0",
            "goto_instrument": "6.11.0 (cbmc-6.11.0)",
            "solvers": [{"name": "cadical", "version": None}],
        },
        "coverage": {"enabled": False},
        "harness_metadata": [
            {
                "pretty_name": HARNESS_NAME,
                "mangled_name": "harness::vkit_relational",
                "crate_name": "harness",
                "source": {"file": "harness.rs", "start_line": 4, "end_line": 9},
                "attributes": {"kind": "Proof", "should_panic": False},
                "contract": {
                    "contracted_function_name": None,
                    "recursion_tracker": None,
                },
                "has_loop_contracts": False,
                "is_automatically_generated": False,
                "is_bounded": False,
                "is_ctor_based": False,
            }
        ],
        "error_details": [error_entry],
        "property_details": [
            {"harness_id": HARNESS_NAME, "property_details": counts}
        ],
        "cbmc": [
            {
                "harness_id": HARNESS_NAME,
                "cbmc_metadata": {
                    "version": "6.11.0",
                    "os_info": "x86_64 linux unix",
                },
                "configuration": {"solver": "cadical", "object_bits": 16},
            }
        ],
        "verification_results": {
            "summary": {
                "status": "completed",
                "total_harnesses": 1,
                "executed": 1,
                "successful": 0 if has_non_success else 1,
                "failed": 1 if has_non_success else 0,
            },
            "results": [
                {
                    "harness_id": HARNESS_NAME,
                    "status": row_status,
                    "checks": checks,
                }
            ],
        },
    }


def oracle_classify(case: TranscriptTuple) -> KaniOutcome:
    """Independent mathematical specification of Kani transcript classification."""
    statuses = case.all_statuses()
    assertions = case.assertion_statuses()

    # Any Undetermined or SolverError property forces unknown.
    if any(status in {"Undetermined", "SolverError"} for status in statuses):
        return KaniOutcome("unknown", "incomplete_or_undetermined_properties")

    # No Failure property anywhere.
    if "Failure" not in statuses:
        if "Unreachable" in statuses:
            return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
        if case.exit_code == 0:
            return KaniOutcome("verified", "all_qualified_properties_succeeded")
        return KaniOutcome("unknown", "exit_status_mismatch")

    # At least one Failure property exists.
    failing_sources = [
        src for src in (case.base_source, case.candidate_source) if src == "Failure"
    ]
    failing_assertions = [
        name for name in HARNESS_ASSERTIONS if assertions[name] == "Failure"
    ]
    total_failures = len(failing_sources) + len(failing_assertions)

    if (
        case.exit_code == 1
        and total_failures == 1
        and len(failing_assertions) == 1
        and failing_assertions[0] in TARGET_ASSERTIONS
        and assertions["base_observation_model"] == "Success"
        and assertions["candidate_observation_model"] == "Success"
    ):
        failed_name = failing_assertions[0]
        failed_line = ASSERTION_LINES[failed_name]
        source_unreachable = (
            case.base_source == "Unreachable"
            or case.candidate_source == "Unreachable"
        )
        earlier_or_equal_assertion_unreachable = any(
            assertions[name] == "Unreachable" and ASSERTION_LINES[name] <= failed_line
            for name in HARNESS_ASSERTIONS
        )
        if source_unreachable or earlier_or_equal_assertion_unreachable:
            return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
        return KaniOutcome("proof_rejected", f"named_{failed_name}_assertion_failed")

    if failing_sources:
        return KaniOutcome("unknown", "safety_property_failed")
    if any(assertions[name] == "Failure" for name in OBSERVATION_ASSERTIONS):
        return KaniOutcome("unknown", "observation_model_failed")
    if "Unreachable" in statuses:
        return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
    return KaniOutcome("unknown", "model_or_safety_property_failed")


def _encode(doc: dict[str, object]) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def build_counter_mismatch_cases() -> list[CounterMismatchCase]:
    """Generate counter-mismatch and schema-consistency edge cases over two baselines."""
    baselines: list[tuple[str, TranscriptTuple]] = [
        (
            "verified_baseline",
            TranscriptTuple(
                base_observation_model="Success",
                candidate_observation_model="Success",
                candidate_standing_intent="Success",
                relational_policy="Success",
                base_source="Success",
                candidate_source="Success",
                exit_code=0,
            ),
        ),
        (
            "proof_rejected_baseline",
            TranscriptTuple(
                base_observation_model="Success",
                candidate_observation_model="Success",
                candidate_standing_intent="Success",
                relational_policy="Failure",
                base_source="Success",
                candidate_source="Success",
                exit_code=1,
            ),
        ),
    ]

    cases: list[CounterMismatchCase] = []
    for label, base_tuple in baselines:
        base_doc = build_transcript_doc(base_tuple)

        def add_doc_case(name: str, mutator: Callable[[dict[str, object]], None]) -> None:
            mutated = copy.deepcopy(base_doc)
            mutator(mutated)
            cases.append(
                CounterMismatchCase(
                    name=f"{label}:{name}",
                    baseline=label,
                    raw=_encode(mutated),
                    exit_code=base_tuple.exit_code,
                    expected_reason="malformed_or_inconsistent_export",
                )
            )

        add_doc_case(
            "total_properties_plus_one",
            lambda d: d["property_details"][0]["property_details"].update(
                total_properties=d["property_details"][0]["property_details"][
                    "total_properties"
                ]
                + 1
            ),
        )
        add_doc_case(
            "total_properties_minus_one",
            lambda d: d["property_details"][0]["property_details"].update(
                total_properties=d["property_details"][0]["property_details"][
                    "total_properties"
                ]
                - 1
            ),
        )
        add_doc_case(
            "passed_plus_one",
            lambda d: d["property_details"][0]["property_details"].update(
                passed=d["property_details"][0]["property_details"]["passed"] + 1
            ),
        )
        add_doc_case(
            "passed_minus_one",
            lambda d: d["property_details"][0]["property_details"].update(
                passed=d["property_details"][0]["property_details"]["passed"] - 1
            ),
        )
        add_doc_case(
            "failed_mismatch",
            lambda d: d["property_details"][0]["property_details"].update(
                failed=d["property_details"][0]["property_details"]["failed"] + 1
            ),
        )
        add_doc_case(
            "unreachable_mismatch",
            lambda d: d["property_details"][0]["property_details"].update(unreachable=1),
        )
        add_doc_case(
            "undetermined_mismatch",
            lambda d: d["property_details"][0]["property_details"].update(
                undetermined=1
            ),
        )
        add_doc_case(
            "solver_error_mismatch",
            lambda d: d["property_details"][0]["property_details"].update(
                solver_error=1
            ),
        )
        for zero_field in ("satisfied", "unsatisfiable", "covered", "uncovered"):
            add_doc_case(
                f"nonzero_{zero_field}",
                lambda d, f=zero_field: d["property_details"][0][
                    "property_details"
                ].update({f: 1}),
            )
        add_doc_case(
            "row_status_flipped",
            lambda d: d["verification_results"]["results"][0].update(
                status=(
                    "Failure"
                    if d["verification_results"]["results"][0]["status"] == "Success"
                    else "Success"
                )
            ),
        )
        add_doc_case(
            "summary_successful_flipped",
            lambda d: d["verification_results"]["summary"].update(
                successful=1 - d["verification_results"]["summary"]["successful"]
            ),
        )
        add_doc_case(
            "summary_failed_flipped",
            lambda d: d["verification_results"]["summary"].update(
                failed=1 - d["verification_results"]["summary"]["failed"]
            ),
        )
        add_doc_case(
            "error_has_errors_flipped",
            lambda d: d["error_details"][0].update(
                has_errors=not d["error_details"][0]["has_errors"]
            ),
        )
        add_doc_case(
            "unknown_status_error_string",
            lambda d: d["verification_results"]["results"][0]["checks"][0].update(
                status="ERROR"
            ),
        )
        add_doc_case(
            "duplicate_property_id",
            lambda d: d["verification_results"]["results"][0]["checks"][1].update(id=1),
        )
        add_doc_case(
            "bool_counter_passed",
            lambda d: d["property_details"][0]["property_details"].update(passed=True),
        )

        raw_base = _encode(base_doc)
        cases.append(
            CounterMismatchCase(
                name=f"{label}:bool_exit_code",
                baseline=label,
                raw=raw_base,
                exit_code=bool(base_tuple.exit_code),
                expected_reason="invalid_decoder_input",
            )
        )

    return cases


def _check_safety_invariants(case: TranscriptTuple, actual: KaniOutcome) -> list[Violation]:
    violations: list[Violation] = []
    statuses = case.all_statuses()
    assertions = case.assertion_statuses()
    case_desc = repr(case)

    # Invariant 1: Never verified unless every property is Success and exit_code == 0.
    if actual.kind == "verified":
        if any(status != "Success" for status in statuses) or case.exit_code != 0:
            violations.append(
                Violation(
                    invariant="never_verified_unless_all_success_and_exit_zero",
                    case=case_desc,
                    expected="not verified",
                    actual=repr(actual),
                )
            )

    # Invariant 2: Never proof_rejected without a genuine failed target assertion.
    if actual.kind == "proof_rejected":
        has_failed_target = any(
            assertions[name] == "Failure" for name in TARGET_ASSERTIONS
        )
        has_failed_source = (
            case.base_source == "Failure" or case.candidate_source == "Failure"
        )
        has_failed_observation = any(
            assertions[name] == "Failure" for name in OBSERVATION_ASSERTIONS
        )
        if (
            not has_failed_target
            or has_failed_source
            or has_failed_observation
            or case.exit_code != 1
        ):
            violations.append(
                Violation(
                    invariant="never_proof_rejected_without_genuine_failed_target",
                    case=case_desc,
                    expected="not proof_rejected",
                    actual=repr(actual),
                )
            )

    # Invariant 3: Never claim source_proof or approval_authority.
    payload = actual.to_json()
    if payload["source_proof"] is not False or payload["approval_authority"] is not False:
        violations.append(
            Violation(
                invariant="never_claims_source_proof_or_approval_authority",
                case=case_desc,
                expected="source_proof=False, approval_authority=False",
                actual=repr(payload),
            )
        )

    return violations


def verify_classifier(
    classifier: ClassifierFn,
    *,
    counter_cases: list[CounterMismatchCase],
    stop_on_first_failure: bool = False,
) -> dict[str, object]:
    """Run exhaustive verification of a classifier against the oracle and invariants."""
    violations: list[Violation] = []
    kind_counts: dict[str, int] = {"verified": 0, "proof_rejected": 0, "unknown": 0}
    reason_counts: dict[str, int] = {}
    tuples_checked = 0
    counter_cases_checked = 0

    for mismatch in counter_cases:
        actual = classifier(
            mismatch.raw,
            exit_code=mismatch.exit_code,
            harness=CANONICAL_HARNESS_BYTES,
        )
        counter_cases_checked += 1
        if actual.kind != "unknown" or actual.reason != mismatch.expected_reason:
            violations.append(
                Violation(
                    invariant="counter_mismatch_fails_closed_to_unknown",
                    case=mismatch.name,
                    expected=f"KaniOutcome('unknown', '{mismatch.expected_reason}')",
                    actual=repr(actual),
                )
            )
            if stop_on_first_failure:
                break

    if not (stop_on_first_failure and violations):
        for (
            base_obs,
            cand_obs,
            cand_intent,
            rel_policy,
            base_src,
            cand_src,
        ) in itertools.product(
            STATUSES,
            STATUSES,
            STATUSES,
            STATUSES,
            STATUSES,
            STATUSES,
        ):
            base_case = TranscriptTuple(
                base_observation_model=base_obs,
                candidate_observation_model=cand_obs,
                candidate_standing_intent=cand_intent,
                relational_policy=rel_policy,
                base_source=base_src,
                candidate_source=cand_src,
                exit_code=0,
            )
            raw = _encode(build_transcript_doc(base_case))
            failed_fast = False
            for exit_code in EXIT_CODES:
                case = (
                    base_case
                    if exit_code == 0
                    else TranscriptTuple(
                        base_observation_model=base_obs,
                        candidate_observation_model=cand_obs,
                        candidate_standing_intent=cand_intent,
                        relational_policy=rel_policy,
                        base_source=base_src,
                        candidate_source=cand_src,
                        exit_code=exit_code,
                    )
                )
                actual = classifier(
                    raw, exit_code=exit_code, harness=CANONICAL_HARNESS_BYTES
                )
                expected = oracle_classify(case)
                tuples_checked += 1

                kind_counts[actual.kind] = kind_counts.get(actual.kind, 0) + 1
                reason_counts[actual.reason] = reason_counts.get(actual.reason, 0) + 1

                safety_violations = _check_safety_invariants(case, actual)
                if safety_violations:
                    violations.extend(safety_violations)
                    if stop_on_first_failure:
                        failed_fast = True
                        break

                if actual != expected:
                    violations.append(
                        Violation(
                            invariant="oracle_equivalence",
                            case=repr(case),
                            expected=repr(expected),
                            actual=repr(actual),
                        )
                    )
                    if stop_on_first_failure:
                        failed_fast = True
                        break
            if failed_fast:
                break

    return {
        "ok": len(violations) == 0,
        "tuples_checked": tuples_checked,
        "counter_cases_checked": counter_cases_checked,
        "kind_counts": kind_counts,
        "reason_counts": reason_counts,
        "violations": [
            {
                "invariant": v.invariant,
                "case": v.case,
                "expected": v.expected,
                "actual": v.actual,
            }
            for v in violations
        ],
    }


def _mutant_ignore_exit_code_on_verified(
    raw: bytes, *, exit_code: int, harness: bytes
) -> KaniOutcome:
    """Negative control 1: treats exit_code != 0 as verified when all checks pass."""
    outcome = classify_kani_result(raw, exit_code=exit_code, harness=harness)
    if outcome == KaniOutcome("unknown", "exit_status_mismatch"):
        return KaniOutcome("verified", "all_qualified_properties_succeeded")
    return outcome


def _mutant_reject_on_safety_property_failure(
    raw: bytes, *, exit_code: int, harness: bytes
) -> KaniOutcome:
    """Negative control 2: misclassifies a source safety failure as proof_rejected."""
    outcome = classify_kani_result(raw, exit_code=exit_code, harness=harness)
    if outcome == KaniOutcome("unknown", "safety_property_failed") and exit_code == 1:
        return KaniOutcome("proof_rejected", "named_relational_policy_assertion_failed")
    return outcome


def _mutant_ignore_property_counter_mismatch(
    raw: bytes, *, exit_code: int, harness: bytes
) -> KaniOutcome:
    """Negative control 3: reconciles mismatched property counters instead of failing closed."""
    outcome = classify_kani_result(raw, exit_code=exit_code, harness=harness)
    if outcome != KaniOutcome("unknown", "malformed_or_inconsistent_export"):
        return outcome
    try:
        doc = json.loads(raw.decode("utf-8"))
        checks = doc["verification_results"]["results"][0]["checks"]
        statuses = [check["status"] for check in checks]
        doc["property_details"][0]["property_details"] = {
            "total_properties": len(checks),
            "passed": statuses.count("Success"),
            "failed": statuses.count("Failure"),
            "unreachable": statuses.count("Unreachable"),
            "undetermined": statuses.count("Undetermined"),
            "solver_error": statuses.count("SolverError"),
            "satisfied": 0,
            "unsatisfiable": 0,
            "covered": 0,
            "uncovered": 0,
        }
        repaired = _encode(doc)
    except Exception:
        return outcome
    return classify_kani_result(repaired, exit_code=exit_code, harness=harness)


NEGATIVE_CONTROL_MUTANTS: tuple[tuple[str, ClassifierFn], ...] = (
    ("ignore_exit_code_on_verified", _mutant_ignore_exit_code_on_verified),
    ("reject_on_safety_property_failure", _mutant_reject_on_safety_property_failure),
    ("ignore_property_counter_mismatch", _mutant_ignore_property_counter_mismatch),
)


def run() -> dict[str, object]:
    """Execute the exhaustive formal check and prove all negative-control mutants fail."""
    counter_cases = build_counter_mismatch_cases()
    real_report = verify_classifier(
        classify_kani_result,
        counter_cases=counter_cases,
        stop_on_first_failure=False,
    )

    mutant_reports: list[dict[str, object]] = []
    all_mutants_caught = True
    for name, mutant_fn in NEGATIVE_CONTROL_MUTANTS:
        report = verify_classifier(
            mutant_fn,
            counter_cases=counter_cases,
            stop_on_first_failure=True,
        )
        caught = not bool(report["ok"])
        if not caught:
            all_mutants_caught = False
        first_violation = report["violations"][0] if report["violations"] else None
        mutant_reports.append(
            {
                "name": name,
                "caught": caught,
                "first_violation": first_violation,
            }
        )

    expected_tuple_count = (len(STATUSES) ** 6) * len(EXIT_CODES)
    ok = (
        bool(real_report["ok"])
        and real_report["tuples_checked"] == expected_tuple_count
        and real_report["counter_cases_checked"] == len(counter_cases)
        and all_mutants_caught
    )

    return {
        "ok": ok,
        "target": "src/vkit/rust/kani_result.py::classify_kani_result",
        "status_domain": list(STATUSES),
        "exit_codes": list(EXIT_CODES),
        "tuples_checked": real_report["tuples_checked"],
        "counter_cases_checked": real_report["counter_cases_checked"],
        "total_cases_checked": int(real_report["tuples_checked"])
        + int(real_report["counter_cases_checked"]),
        "kind_counts": real_report["kind_counts"],
        "reason_counts": real_report["reason_counts"],
        "violations": real_report["violations"],
        "negative_controls": mutant_reports,
    }


def main() -> int:
    result = run()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
