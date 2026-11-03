from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations, combinations_with_replacement, product
import json
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("x", "y")
OUTPUTS = ("bad", "good")
OBSERVATIONS = (*OUTPUTS, "alien")
REPETITIONS = 2


@dataclass(frozen=True)
class PolicyCase:
    intent: Any
    policy: Any
    allowed: dict[str, frozenset[str]]
    guards: dict[str, tuple[tuple[str, frozenset[str]], ...]]
    digest: str


def nonempty_subsets(values: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    values = tuple(values)
    return tuple(
        subset
        for size in range(1, len(values) + 1)
        for subset in combinations(values, size)
    )


def make_intent(allowed: dict[str, tuple[str, ...]]) -> Any:
    from vkit.certification import IntentRevision

    hashes = {"codec": "a" * 64, "observation": "b" * 64, "environment": "c" * 64}
    domain = {"binding": "finite::convert", "inputs": list(INPUTS),
              "observations": list(OUTPUTS), **{f"{role}_sha256": digest for role, digest in hashes.items()}}
    return IntentRevision.from_json({
        "schema_version": 1,
        "requirement": {"source": "requirements.md#convert",
                        "statement": "Return an allowed observation for each accepted input."},
        "contract": {
            "domain": domain,
            "admissible_inputs": list(INPUTS),
            "allowed_outputs": [{"input": token, "outputs": list(allowed[token])} for token in INPUTS],
        },
        "artifacts": {"binding": {"path": "binding.json", "sha256": "d" * 64},
                      **{role: {"path": f"{role}.json", "sha256": digest}
                         for role, digest in hashes.items()}},
    })


def guard_maps(allowed: dict[str, tuple[str, ...]]) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], ...]:
    choices = [
        (None, *nonempty_subsets(allowed[token]))
        for token in INPUTS
    ]
    maps = []
    for rows in product(*choices):
        mapping = tuple(
            (token, row)
            for token, row in zip(INPUTS, rows, strict=True)
            if row is not None
        )
        if mapping:
            maps.append(mapping)
    return tuple(maps)


def guard_conflict(guards: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...]) -> bool:
    intersections: dict[str, set[str]] = {}
    for guard in guards:
        for token, outputs in guard:
            if token in intersections:
                intersections[token].intersection_update(outputs)
                if not intersections[token]:
                    return True
            else:
                intersections[token] = set(outputs)
    return False


def policy_json(
    intent: Any,
    guards: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent.digest,
        "guards": [
            {
                "id": f"g{index}",
                "requirements": [
                    {"input": token, "outputs": list(outputs)}
                    for token, outputs in guard
                ],
            }
            for index, guard in enumerate(guards)
        ],
    }


def policy_cases() -> tuple[tuple[PolicyCase, ...], int, int]:
    from vkit.certification import DifferencePolicy

    cases = []
    candidates = 0
    rejected = 0
    for output_rows in product(nonempty_subsets(OUTPUTS), repeat=len(INPUTS)):
        allowed = dict(zip(INPUTS, output_rows, strict=True))
        intent = make_intent(allowed)
        maps = guard_maps(allowed)
        shapes = [()]
        shapes.extend((item,) for item in maps)
        shapes.extend(combinations_with_replacement(maps, 2))
        for guards in shapes:
            candidates += 1
            raw = policy_json(intent, guards)
            if guard_conflict(guards):
                try:
                    DifferencePolicy.from_json(raw, intent=intent)
                except ValueError as exc:
                    if "overlapping difference guards conflict" not in str(exc):
                        raise
                    rejected += 1
                    continue
                raise AssertionError("conflicting guard intersection was admitted")
            policy = DifferencePolicy.from_json(raw, intent=intent)
            by_input: dict[str, list[tuple[str, frozenset[str]]]] = {
                token: [] for token in INPUTS
            }
            for index, guard in enumerate(guards):
                for token, outputs in guard:
                    by_input[token].append((f"g{index}", frozenset(outputs)))
            normalized = {
                token: tuple(sorted(rows)) for token, rows in by_input.items()
            }
            digest = sha256(json.dumps(
                {"allowed": allowed, "guards": raw["guards"]},
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            cases.append(PolicyCase(
                intent, policy,
                {token: frozenset(values) for token, values in allowed.items()},
                normalized,
                digest,
            ))
    return tuple(cases), candidates, rejected


def accepted_relation(
    base: str,
    candidate: str,
    allowed: frozenset[str],
    guards: tuple[tuple[str, frozenset[str]], ...],
) -> bool:
    return candidate in allowed and (
        all(candidate in outputs for _, outputs in guards)
        if guards else base == candidate or base not in allowed
    )


def classify(
    base: str,
    candidate: str,
    allowed: frozenset[str],
    guards: tuple[tuple[str, frozenset[str]], ...],
) -> str:
    if not accepted_relation(base, candidate, allowed, guards):
        return "violation"
    if guards:
        return "guarded_requirement_met"
    if base not in allowed:
        return "standing_intent_repair"
    return "preserved"


def evidence_fact(row: Any) -> tuple[str, str | None, str | None]:
    complete = (
        row.failure is None
        and len(row.base_values) == REPETITIONS
        and len(row.candidate_values) == REPETITIONS
        and len(set(row.base_values)) == len(set(row.candidate_values)) == 1
    )
    if not complete:
        return "unknown", None, None
    base, candidate = row.base_values[0], row.candidate_values[0]
    if base not in OUTPUTS or candidate not in OUTPUTS:
        return "unknown", base, candidate
    return "decision", base, candidate


def fact_order(fact: tuple[str, str, str | None, str | None]) -> tuple[str, str, str, str]:
    return tuple(value or "" for value in fact)


def oracle(
    case: PolicyCase,
    observations: tuple[Any, ...],
) -> dict[str, Any]:
    by_input = {token: [] for token in INPUTS}
    for observation in observations:
        if observation.input in by_input:
            by_input[observation.input].append(observation)
    facts = []
    missing = set()
    for token in INPUTS:
        rows = by_input[token]
        selected = [row for row in rows if row.selected]
        unique = len(selected) == 1
        if not unique:
            missing.add(token)
            facts.append((token, "unknown", None, None))
        for row in rows:
            kind, base, candidate = evidence_fact(row)
            if kind == "decision":
                kind = classify(base, candidate, case.allowed[token], case.guards[token])
            if row.selected and unique and kind == "unknown":
                missing.add(token)
            include = (
                (row.selected and (unique or kind == "violation"))
                or (not row.selected and kind in {"unknown", "violation"})
            )
            if include:
                facts.append((token, kind, base, candidate))
    complete = bool(INPUTS) and not missing
    if any(fact[1] == "violation" for fact in facts):
        status = "observed_violation"
    elif complete and not any(fact[1] == "unknown" for fact in facts):
        status = "observed_conformance"
    else:
        status = "unknown"
    return {
        "status": status,
        "coverage_complete": complete,
        "missing_inputs": tuple(sorted(missing)),
        "classifications": tuple(sorted(facts, key=fact_order)),
    }


def project_assessment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": raw["status"],
        "coverage_complete": raw["coverage_complete"],
        "missing_inputs": tuple(raw["missing_inputs"]),
        "classifications": tuple(sorted((
            (row["input"], row["kind"], row["base_output"], row["candidate_output"])
            for row in raw["findings"]
        ), key=fact_order)),
    }


def decision_mismatches(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    return [field for field in expected if expected[field] != observed[field]]


def all_sequences() -> tuple[tuple[str, ...], ...]:
    return tuple(product(OBSERVATIONS, repeat=REPETITIONS))


def observation(
    token: str,
    base: tuple[str, ...],
    candidate: tuple[str, ...],
    *,
    selected: bool = True,
    failure: str | None = None,
) -> Any:
    from vkit.certification import PairedObservation

    return PairedObservation(token, base, candidate, failure=failure, selected=selected)


def check_result(
    case: PolicyCase,
    observations: tuple[Any, ...],
    digest: Any,
    counts: Counter[str],
    mismatch_total: list[int],
    mismatches: list[dict[str, Any]],
    label: str,
    evaluator: Any = None,
) -> None:
    expected, observed, differences = compare_tuple(case, observations, evaluator)
    counts[f"{label}:expected:{expected['status']}"] += 1
    counts[f"{label}:observed:{observed['status']}"] += 1
    digest.update(json.dumps({
        "policy": case.digest,
        "observations": [
            {
                "input": row.input,
                "base": row.base_values,
                "candidate": row.candidate_values,
                "selected": row.selected,
                "failure": row.failure,
            }
            for row in observations
        ],
        "expected": expected,
    }, sort_keys=True, separators=(",", ":")).encode())
    if differences:
        mismatch_total[0] += 1
        if len(mismatches) < 20:
            mismatches.append({
                "policy_sha256": case.digest,
                "case": label,
                "observations": [
                    [row.input, list(row.base_values), list(row.candidate_values), row.selected, row.failure]
                    for row in observations
                ],
                "expected": expected,
                "observed": observed,
                "different_fields": differences,
            })


def malformed_sequences() -> tuple[tuple[str, ...], ...]:
    return ((),) + tuple((value,) for value in OBSERVATIONS) + tuple(
        (value, value, value) for value in OBSERVATIONS
    )


def aggregate_cases() -> tuple[tuple[str, tuple[Any, ...]], ...]:
    sample = ("good", "good")
    changed = ("bad", "bad")
    flaky = ("bad", "good")
    return (
        ("missing", ()),
        ("unselected", (observation("x", sample, sample, selected=False),)),
        ("preserved", (observation("x", sample, sample),)),
        ("violation", (observation("x", sample, changed),)),
        ("repair", (observation("x", changed, sample),)),
        ("failed", (observation("x", sample, sample, failure="process_failed"),)),
        ("unstable", (observation("x", sample, flaky),)),
        ("out_of_model", (observation("x", sample, ("alien", "alien")),)),
    )


def public_evaluator(case: PolicyCase, observations: tuple[Any, ...]) -> dict[str, Any]:
    from vkit.certification import assess_differences

    return project_assessment(assess_differences(
        case.intent, case.policy, observations, repetitions=REPETITIONS
    ).to_json())


def compare_tuple(
    case: PolicyCase,
    observations: tuple[Any, ...],
    evaluator: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    expected = oracle(case, observations)
    observed = (evaluator or public_evaluator)(case, observations)
    return expected, observed, decision_mismatches(expected, observed)


def negative_controls(case: PolicyCase) -> dict[str, Any]:
    from vkit.certification import DifferencePolicy

    plain = PolicyCase(case.intent, DifferencePolicy.from_json(
        policy_json(case.intent, ()), intent=case.intent
    ), case.allowed, {token: () for token in INPUTS}, "0" * 64)
    violation = (observation("x", ("good", "good"), ("bad", "bad")),)

    def drop_violation(test_case: PolicyCase, rows: tuple[Any, ...]) -> dict[str, Any]:
        result = public_evaluator(test_case, rows)
        return {
            **result,
            "status": "unknown",
            "classifications": tuple(
                fact for fact in result["classifications"] if fact[1] != "violation"
            ),
        }

    expected, mutant, differences = compare_tuple(plain, violation, drop_violation)
    violation_control = {
        "mutant": "drop a known unguarded violation",
        "decision_rejected": bool(differences),
        "different_fields": differences,
        "expected_status": expected["status"],
        "mutant_status": mutant["status"],
    }
    missing = (
        observation("x", ("good", "good"), ("good", "good")),
    )

    def fill_coverage(test_case: PolicyCase, rows: tuple[Any, ...]) -> dict[str, Any]:
        return {
            **public_evaluator(test_case, rows),
            "status": "observed_conformance",
            "coverage_complete": True,
            "missing_inputs": (),
        }

    expected, mutant, differences = compare_tuple(plain, missing, fill_coverage)
    coverage_control = {
        "mutant": "treat missing selected input as complete coverage",
        "decision_rejected": bool(differences),
        "different_fields": differences,
        "expected_status": expected["status"],
        "mutant_status": mutant["status"],
    }
    return {"controls": [violation_control, coverage_control],
            "decision_rejected": all(item["decision_rejected"] for item in (violation_control, coverage_control))}


def run() -> dict[str, Any]:
    cases, policy_candidates, rejected_policies = policy_cases()
    sequence_pool = all_sequences()
    malformed = malformed_sequences()
    counts: Counter[str] = Counter()
    mismatch_total = [0]
    mismatches: list[dict[str, Any]] = []
    digest = sha256()
    local_count = 0
    full_domain_count = 0
    for case in cases:
        for token in INPUTS:
            for base in sequence_pool:
                for candidate in sequence_pool:
                    for selected in (False, True):
                        for failure in (None, "process_failed"):
                            rows = (observation(token, base, candidate,
                                                selected=selected, failure=failure),)
                            check_result(case, rows, digest, counts, mismatch_total, mismatches, "single")
                            local_count += 1
            for selected in (False, True):
                for short_or_long in malformed:
                    for normal in sequence_pool:
                        for rows in (
                            (observation(token, short_or_long, normal, selected=selected),),
                            (observation(token, normal, short_or_long, selected=selected),),
                        ):
                            check_result(case, rows, digest, counts, mismatch_total, mismatches, "repetition_shape")
                            local_count += 1
            stable = tuple((value, value) for value in OBSERVATIONS)
            selected_rows = []
            for base in stable:
                for candidate in stable:
                    selected_rows.append(observation(token, base, candidate))
            for sample in selected_rows:
                duplicate = (sample, sample)
                check_result(case, duplicate, digest, counts, mismatch_total, mismatches, "duplicate_selected")
                local_count += 1
                for shrink_candidate in ("good", "bad"):
                    shrink = observation(
                        token, ("good", "good"), (shrink_candidate, shrink_candidate),
                        selected=False,
                    )
                    pair = (sample, shrink)
                    check_result(case, pair, digest, counts, mismatch_total, mismatches,
                                 "selected_with_shrink")
                    local_count += 1
            selected_good = observation(token, ("good", "good"), ("good", "good"))
            selected_violation = observation(token, ("good", "good"), ("bad", "bad"))
            selected_failure = observation(
                token, ("good", "good"), ("good", "good"), failure="process_failed",
            )
            selected_flaky = observation(token, ("good", "bad"), ("good", "good"))
            for duplicate in (
                (selected_good, selected_violation),
                (selected_failure, selected_violation),
                (selected_flaky, selected_good),
            ):
                check_result(case, duplicate, digest, counts, mismatch_total, mismatches,
                             "duplicate_selected_interaction")
                local_count += 1
        stable = tuple((value, value) for value in OBSERVATIONS)
        for x_base in stable:
            for x_candidate in stable:
                for y_base in stable:
                    for y_candidate in stable:
                        rows = (
                            observation("x", x_base, x_candidate),
                            observation("y", y_base, y_candidate),
                        )
                        check_result(case, rows, digest, counts, mismatch_total, mismatches, "full_domain")
                        full_domain_count += 1

    aggregate_case = next(
        item for item in cases
        if not item.policy.guards
        and item.allowed == {"x": frozenset({"good"}), "y": frozenset({"good"})}
    )
    aggregate = aggregate_cases()
    aggregate_count = 0
    for (_, left), (_, right) in product(aggregate, repeat=2):
        rows = tuple(
            observation(token, row.base_values,
                        row.candidate_values, selected=row.selected, failure=row.failure)
            for token, group in (("x", left), ("y", right)) for row in group
        )
        check_result(aggregate_case, rows, digest, counts, mismatch_total, mismatches, "aggregate")
        aggregate_count += 1

    controls = negative_controls(aggregate_case)
    checked = local_count + full_domain_count + aggregate_count
    return {
        "schema_version": 1,
        "scope": "exhaustive finite decision tuples for two inputs, two standing outputs, and two repetitions",
        "checker_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "evaluator": "src/vkit/certification.py",
        "evaluator_sha256": sha256((ROOT / "src" / "vkit" / "certification.py").read_bytes()).hexdigest(),
        "reference_model": "verification/certification/Differences.tla",
        "reference_model_sha256": sha256(
            (ROOT / "verification" / "certification" / "Differences.tla").read_bytes()
        ).hexdigest(),
        "selected_tlc_bridge": "scripts/check_difference_decisions.py",
        "dimensions": {
            "inputs": list(INPUTS),
            "standing_output_atoms": list(OUTPUTS),
            "observation_values_including_out_of_model": list(OBSERVATIONS),
            "accepted_output_subsets_per_input": len(nonempty_subsets(OUTPUTS)),
            "contract_combinations": len(nonempty_subsets(OUTPUTS)) ** len(INPUTS),
            "guards_per_policy": [0, 1, 2],
            "guard_row_choices_per_input": "absent or any nonempty subset of standing outputs",
            "repetitions": REPETITIONS,
            "evidence_axes": [
                "selected or shrink-only",
                "process failure or completed observation",
                "stable or flaky observations",
                "correct, short, or long repetition count",
                "missing, unique, duplicate selected inputs",
            ],
            "coverage_and_aggregation": [
                "all inputs selected versus missing selected input",
                "violation dominance over missing and unknown evidence",
            ],
        },
        "policy_candidates": policy_candidates,
        "valid_policies_checked": len(cases),
        "conflicting_guard_policies_rejected": rejected_policies,
        "local_evidence_tuples_checked": local_count,
        "full_domain_output_tuples_checked": full_domain_count,
        "aggregate_precedence_tuples_checked": aggregate_count,
        "decision_tuples_checked": checked,
        "expected_status_counts": {
            key.replace(":expected:", ":", 1): value
            for key, value in sorted(counts.items()) if ":expected:" in key
        },
        "observed_status_counts": {
            key.replace(":observed:", ":", 1): value
            for key, value in sorted(counts.items()) if ":observed:" in key
        },
        "observed_decision_count": checked,
        "mismatch_count": mismatch_total[0],
        "mismatches": mismatches,
        "comparator_negative_controls": controls,
        "tuple_space_sha256": digest.hexdigest(),
        "source_proof": False,
        "approval_authority": False,
        "passed": not mismatches and controls["decision_rejected"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exhaust finite difference decisions against an independent relation oracle.")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / "finite-decisions" / "report.json")
    args = parser.parse_args()
    report = run()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "passed", "policy_candidates", "valid_policies_checked",
        "conflicting_guard_policies_rejected", "decision_tuples_checked",
        "mismatch_count", "comparator_negative_controls", "tuple_space_sha256",
    )} | {"report": str(output)}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
