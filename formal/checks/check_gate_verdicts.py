"""Exhaustive real-code formal checker for vkit.core.state.gate."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vkit.core import state as gate_state
from vkit.core.state import CheckState, FormalState, Freshness

ROOT = Path(__file__).resolve().parents[2]

EFFECT_ORDER = ("reject", "block", "scope")

# Independent specification of the DOUBTS catalog (field, effect, clearing).
SPEC_DOUBTS: tuple[tuple[str, str, str], ...] = (
    ("fresh_fail", "reject", "fix the subject until the check passes"),
    ("not_approved", "reject", "authority revision: accept the definition"),
    ("obligation_inventory", "reject", "authority revision: accept the manifest to bind the inventory"),
    ("obligation_removed", "reject", "authority revision: re-accept the manifest"),
    ("fresh_blocked", "block", "clear the run blocker and re-run"),
    ("stale", "block", "re-run the check on the current inputs"),
    ("missing", "block", "run the check to produce evidence"),
    ("blocked", "block", "clear the reported blocker and re-run"),
    ("uncovered_change", "scope", "extend coverage or declare does_not_establish"),
    ("weakened_check", "reject", "fix the evaluator or accept the weakening with both runs displayed"),
    ("incomparable", "block", "re-accept the reference"),
)

BLOCKED_FRESHNESS_PRECEDENCE: tuple[Freshness, ...] = (
    Freshness.FRESH_BLOCKED,
    Freshness.BLOCKED,
    Freshness.STALE,
    Freshness.MISSING,
)

ALL_FRESHNESS: tuple[Freshness, ...] = (
    Freshness.FRESH_PASS,
    Freshness.FRESH_FAIL,
    Freshness.FRESH_BLOCKED,
    Freshness.STALE,
    Freshness.MISSING,
    Freshness.NOT_APPROVED,
    Freshness.BLOCKED,
)


@dataclass(frozen=True)
class CheckSpecInput:
    check_id: str
    freshness: Freshness
    running: tuple[str, ...] = ()
    label: str = ""

    def to_state(self) -> CheckState:
        return CheckState(
            check=SimpleNamespace(id=self.check_id),
            digest="d",
            key="k",
            inputs=None,  # type: ignore[arg-type]
            freshness=self.freshness,
            current={"run_id": "run-current"} if self.freshness in (
                Freshness.FRESH_PASS, Freshness.FRESH_FAIL, Freshness.FRESH_BLOCKED
            ) else None,
            last=None,
            running=self.running,
        )


@dataclass(frozen=True)
class FormalSpecInput:
    component_id: str
    required: bool
    freshness: Freshness
    running: tuple[str, ...] = ()
    label: str = ""

    def to_state(self) -> FormalState:
        return FormalState(
            component=SimpleNamespace(id=self.component_id, required=self.required),
            key="fk",
            freshness=self.freshness,
            current={"run_id": "formal-current"} if self.freshness in (
                Freshness.FRESH_PASS, Freshness.FRESH_FAIL, Freshness.FRESH_BLOCKED
            ) else None,
            last=None,
            running=self.running,
        )


@dataclass(frozen=True)
class GateInputTuple:
    label: str
    checks: tuple[CheckSpecInput, ...]
    formal: tuple[FormalSpecInput, ...]
    obligations: dict[str, Any] | None
    uncovered: tuple[str, ...]
    comparisons: dict[str, dict[str, Any]] | None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "checks": [
                {"id": c.check_id, "freshness": c.freshness.value, "running": list(c.running)}
                for c in self.checks
            ],
            "formal": [
                {
                    "id": f.component_id,
                    "required": f.required,
                    "freshness": f.freshness.value,
                    "running": list(f.running),
                }
                for f in self.formal
            ],
            "obligations": self.obligations,
            "uncovered": list(self.uncovered),
            "comparisons": self.comparisons,
        }


def check_slot_domain() -> tuple[tuple[str, tuple[CheckSpecInput, ...]], ...]:
    """Finite domain for the primary check slot: absent, all Freshness values, and running."""
    slots: list[tuple[str, tuple[CheckSpecInput, ...]]] = [("absent", ())]
    for freshness in ALL_FRESHNESS:
        slots.append((freshness.value, (CheckSpecInput("c1", freshness, (), freshness.value),)))
    slots.append((
        "running",
        (CheckSpecInput("c1", Freshness.MISSING, ("run-active",), "running"),),
    ))
    return tuple(slots)


def formal_slot_domain() -> tuple[tuple[str, tuple[FormalSpecInput, ...]], ...]:
    """Finite domain for the formal component slot: absent, all required states, and optional states."""
    slots: list[tuple[str, tuple[FormalSpecInput, ...]]] = [("absent", ())]
    for freshness in ALL_FRESHNESS:
        slots.append((
            f"required:{freshness.value}",
            (FormalSpecInput("f1", True, freshness, (), f"required:{freshness.value}"),),
        ))
    for freshness in (Freshness.FRESH_PASS, Freshness.FRESH_FAIL, Freshness.NOT_APPROVED):
        slots.append((
            f"optional:{freshness.value}",
            (FormalSpecInput("f1", False, freshness, (), f"optional:{freshness.value}"),),
        ))
    return tuple(slots)


def obligation_modes() -> tuple[str, ...]:
    return (
        "none",
        "bound",
        "removed_check",
        "removed_formal",
        "dropped_absent",
        "dropped_present",
    )


def build_obligations(
    mode: str,
    checks: tuple[CheckSpecInput, ...],
    formal: tuple[FormalSpecInput, ...],
) -> dict[str, Any] | None:
    if mode == "none":
        return None
    base_checks = {c.check_id: "d" for c in checks}
    base_formal = {f.component_id: "d" for f in formal if f.required}
    if mode == "bound":
        return {"checks": base_checks, "formal_required": base_formal, "dropped": {}}
    if mode == "removed_check":
        return {"checks": {**base_checks, "c_removed": "d"}, "formal_required": base_formal, "dropped": {}}
    if mode == "removed_formal":
        return {"checks": base_checks, "formal_required": {**base_formal, "f1": "d"}, "dropped": {}}
    if mode == "dropped_absent":
        return {"checks": base_checks, "formal_required": base_formal, "dropped": {"c_dropped": "r1"}}
    if mode == "dropped_present":
        return {"checks": base_checks, "formal_required": base_formal, "dropped": {"c1": "r1"}}
    raise ValueError(f"unknown obligation mode: {mode}")


def comparison_domain() -> tuple[tuple[str, dict[str, dict[str, Any]] | None], ...]:
    return (
        ("none", None),
        ("unchanged", {"c1": {"outcome": "unchanged"}}),
        ("fixed_evaluator", {"c1": {"outcome": "fixed_evaluator"}}),
        ("incomparable", {"c1": {"outcome": "incomparable"}}),
        ("weakened_check", {"c1": {"outcome": "weakened_check"}}),
        ("weakened_and_incomparable", {
            "c1": {"outcome": "weakened_check"},
            "c2": {"outcome": "incomparable"},
        }),
    )


def scope_domain() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        ("full_tree", ()),
        ("scoped_one_uncovered", ("README.md",)),
        ("scoped_two_uncovered", ("README.md", "docs/spec.md")),
    )


def oracle(case: GateInputTuple) -> dict[str, Any]:
    """Independent mathematical specification of the gate verdict and DOUBTS precedence algebra."""
    required_formal = tuple(f for f in case.formal if f.required)
    all_required_freshness = tuple(c.freshness for c in case.checks) + tuple(
        f.freshness for f in required_formal
    )

    comparisons = case.comparisons or {}
    weak = sorted(cid for cid, out in comparisons.items() if out["outcome"] == "weakened_check")
    incomp = sorted(cid for cid, out in comparisons.items() if out["outcome"] == "incomparable")

    ghost_ids: tuple[str, ...] = ()
    if weak:
        verdict = "REJECTED"
        reason_token = "weakened_check"
    elif incomp:
        verdict = "BLOCKED"
        reason_token = "incomparable"
    else:
        if case.obligations is not None:
            registered_checks = {c.check_id for c in case.checks}
            required_formal_ids = {f.component_id for f in required_formal}
            removed = set(case.obligations.get("checks", ())) - registered_checks
            removed |= set(case.obligations.get("formal_required", ())) - required_formal_ids
            removed |= set(case.obligations.get("dropped", ())) - registered_checks
            ghost_ids = tuple(sorted(removed))

        if not case.checks and not required_formal and not ghost_ids:
            verdict = "BLOCKED"
            reason_token = "no checks are registered"
        elif case.obligations is None and (case.checks or required_formal):
            verdict = "REJECTED"
            reason_token = "obligation_inventory"
        elif ghost_ids:
            verdict = "REJECTED"
            reason_token = "obligation_removed"
        elif Freshness.FRESH_FAIL in all_required_freshness:
            verdict = "REJECTED"
            reason_token = "fresh_fail"
        elif Freshness.NOT_APPROVED in all_required_freshness:
            verdict = "REJECTED"
            reason_token = "not_approved"
        elif all(fresh is Freshness.FRESH_PASS for fresh in all_required_freshness):
            if case.uncovered:
                verdict = "READY_SCOPED"
                reason_token = "uncovered_change"
            else:
                verdict = "READY"
                reason_token = "every required check passed against the current inputs"
        else:
            stuck = set(all_required_freshness)
            verdict = "BLOCKED"
            reason_token = next(
                fresh.value for fresh in BLOCKED_FRESHNESS_PRECEDENCE if fresh in stuck
            )

    active_fields = {
        fresh.value for fresh in all_required_freshness if fresh is not Freshness.FRESH_PASS
    }
    active_fields.add(reason_token)
    doubts = tuple(
        sorted(
            (entry for entry in SPEC_DOUBTS if entry[0] in active_fields),
            key=lambda entry: EFFECT_ORDER.index(entry[1]),
        )
    )
    return {
        "verdict": verdict,
        "reason_token": reason_token,
        "clearing": doubts[0][2] if doubts else None,
        "doubt_fields": tuple(entry[0] for entry in doubts),
        "doubt_effects": tuple(entry[1] for entry in doubts),
        "ghost_ids": ghost_ids,
    }


def project_gate(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": raw["verdict"],
        "reason_token": raw["reason"].split(":", 1)[0],
        "clearing": raw["clearing"],
        "doubt_fields": tuple(item["field"] for item in raw["doubts"]),
        "doubt_effects": tuple(item["effect"] for item in raw["doubts"]),
        "ghost_ids": tuple(
            item["id"] for item in raw["checks"] if item["state"] == "obligation_removed"
        ),
    }


def public_evaluator(case: GateInputTuple) -> dict[str, Any]:
    return project_gate(
        gate_state.gate(
            states=[c.to_state() for c in case.checks],
            formal=[f.to_state() for f in case.formal],
            obligations=case.obligations,
            uncovered=case.uncovered,
            comparisons=case.comparisons,
        )
    )


def decision_mismatches(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    return [field for field in expected if expected[field] != observed[field]]


def compare_tuple(
    case: GateInputTuple,
    evaluator: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    expected = oracle(case)
    observed = (evaluator or public_evaluator)(case)
    return expected, observed, decision_mismatches(expected, observed)


def negative_controls() -> dict[str, Any]:
    """Deliberate broken-verdict mutants proving the checker rejects each failure mode."""
    controls: list[dict[str, Any]] = []

    # Mutant 1: Ignore obligation_removed (dropped obligation) and reach READY.
    dropped_case = GateInputTuple(
        label="mutant:ignore_obligation_removed",
        checks=(CheckSpecInput("c1", Freshness.FRESH_PASS),),
        formal=(),
        obligations={"checks": {"c1": "d"}, "formal_required": {}, "dropped": {"c_dropped": "r1"}},
        uncovered=(),
        comparisons=None,
    )

    def mutant_ignore_obligation_removed(case: GateInputTuple) -> dict[str, Any]:
        sanitized = {
            "checks": {c.check_id: "d" for c in case.checks},
            "formal_required": {f.component_id: "d" for f in case.formal if f.required},
            "dropped": {},
        }
        return project_gate(
            gate_state.gate(
                states=[c.to_state() for c in case.checks],
                formal=[f.to_state() for f in case.formal],
                obligations=sanitized,
                uncovered=case.uncovered,
                comparisons=case.comparisons,
            )
        )

    expected, mutant, diffs = compare_tuple(dropped_case, mutant_ignore_obligation_removed)
    controls.append({
        "mutant": "ignore obligation_removed when a dropped check is absent",
        "decision_rejected": bool(diffs),
        "different_fields": diffs,
        "expected_verdict": expected["verdict"],
        "mutant_verdict": mutant["verdict"],
    })

    # Mutant 2: Allow READY when a required formal goal is NOT_APPROVED.
    unapproved_formal_case = GateInputTuple(
        label="mutant:allow_ready_on_unapproved_formal",
        checks=(CheckSpecInput("c1", Freshness.FRESH_PASS),),
        formal=(FormalSpecInput("f1", True, Freshness.NOT_APPROVED),),
        obligations={"checks": {"c1": "d"}, "formal_required": {"f1": "d"}, "dropped": {}},
        uncovered=(),
        comparisons=None,
    )

    def mutant_allow_ready_unapproved_formal(case: GateInputTuple) -> dict[str, Any]:
        promoted_formal = [
            FormalSpecInput(f.component_id, f.required, Freshness.FRESH_PASS, f.running).to_state()
            for f in case.formal
        ]
        return project_gate(
            gate_state.gate(
                states=[c.to_state() for c in case.checks],
                formal=promoted_formal,
                obligations=case.obligations,
                uncovered=case.uncovered,
                comparisons=case.comparisons,
            )
        )

    expected, mutant, diffs = compare_tuple(
        unapproved_formal_case, mutant_allow_ready_unapproved_formal
    )
    controls.append({
        "mutant": "allow READY when a required formal goal is not_approved",
        "decision_rejected": bool(diffs),
        "different_fields": diffs,
        "expected_verdict": expected["verdict"],
        "mutant_verdict": mutant["verdict"],
    })

    # Mutant 3: Ignore weakened_check comparison and allow READY.
    weakened_case = GateInputTuple(
        label="mutant:ignore_weakened_check",
        checks=(CheckSpecInput("c1", Freshness.FRESH_PASS),),
        formal=(),
        obligations={"checks": {"c1": "d"}, "formal_required": {}, "dropped": {}},
        uncovered=(),
        comparisons={"c1": {"outcome": "weakened_check"}},
    )

    def mutant_ignore_weakened_check(case: GateInputTuple) -> dict[str, Any]:
        return project_gate(
            gate_state.gate(
                states=[c.to_state() for c in case.checks],
                formal=[f.to_state() for f in case.formal],
                obligations=case.obligations,
                uncovered=case.uncovered,
                comparisons=None,
            )
        )

    expected, mutant, diffs = compare_tuple(weakened_case, mutant_ignore_weakened_check)
    controls.append({
        "mutant": "ignore weakened_check comparison and allow READY",
        "decision_rejected": bool(diffs),
        "different_fields": diffs,
        "expected_verdict": expected["verdict"],
        "mutant_verdict": mutant["verdict"],
    })

    # Mutant 4: Return READY instead of READY_SCOPED when uncovered paths exist.
    uncovered_case = GateInputTuple(
        label="mutant:ignore_uncovered_paths",
        checks=(CheckSpecInput("c1", Freshness.FRESH_PASS),),
        formal=(),
        obligations={"checks": {"c1": "d"}, "formal_required": {}, "dropped": {}},
        uncovered=("README.md",),
        comparisons=None,
    )

    def mutant_ignore_uncovered_paths(case: GateInputTuple) -> dict[str, Any]:
        return project_gate(
            gate_state.gate(
                states=[c.to_state() for c in case.checks],
                formal=[f.to_state() for f in case.formal],
                obligations=case.obligations,
                uncovered=(),
                comparisons=case.comparisons,
            )
        )

    expected, mutant, diffs = compare_tuple(uncovered_case, mutant_ignore_uncovered_paths)
    controls.append({
        "mutant": "return READY instead of READY_SCOPED when uncovered paths exist",
        "decision_rejected": bool(diffs),
        "different_fields": diffs,
        "expected_verdict": expected["verdict"],
        "mutant_verdict": mutant["verdict"],
    })

    # Mutant 5: Invert DOUBTS effect precedence (put block clearing ahead of reject clearing).
    precedence_case = GateInputTuple(
        label="mutant:invert_doubts_precedence",
        checks=(
            CheckSpecInput("c1", Freshness.STALE),
            CheckSpecInput("c2", Freshness.FRESH_FAIL),
        ),
        formal=(),
        obligations={"checks": {"c1": "d", "c2": "d"}, "formal_required": {}, "dropped": {}},
        uncovered=(),
        comparisons=None,
    )

    def mutant_invert_doubts_precedence(case: GateInputTuple) -> dict[str, Any]:
        observed = public_evaluator(case)
        return {
            **observed,
            "doubt_fields": tuple(reversed(observed["doubt_fields"])),
            "doubt_effects": tuple(reversed(observed["doubt_effects"])),
            "clearing": "re-run the check on the current inputs",
        }

    expected, mutant, diffs = compare_tuple(precedence_case, mutant_invert_doubts_precedence)
    controls.append({
        "mutant": "invert DOUBTS precedence by placing block clearing ahead of reject clearing",
        "decision_rejected": bool(diffs),
        "different_fields": diffs,
        "expected_verdict": expected["verdict"],
        "mutant_verdict": mutant["verdict"],
    })

    return {
        "controls": controls,
        "decision_rejected": all(item["decision_rejected"] for item in controls),
    }


def _record_tuple(
    case: GateInputTuple,
    digest: Any,
    counts: Counter[str],
    mismatch_total: list[int],
    mismatches: list[dict[str, Any]],
    group: str,
) -> None:
    expected, observed, diffs = compare_tuple(case)
    counts[f"{group}:expected:{expected['verdict']}"] += 1
    counts[f"{group}:observed:{observed['verdict']}"] += 1
    counts[f"reason:{observed['reason_token']}"] += 1
    digest.update(
        json.dumps(
            {"case": case.to_canonical_dict(), "expected": expected},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if diffs:
        mismatch_total[0] += 1
        if len(mismatches) < 20:
            mismatches.append({
                "group": group,
                "case": case.to_canonical_dict(),
                "expected": expected,
                "observed": observed,
                "different_fields": diffs,
            })


def run() -> dict[str, Any]:
    counts: Counter[str] = Counter()
    mismatch_total = [0]
    mismatches: list[dict[str, Any]] = []
    digest = sha256()

    # 1. Full Cartesian matrix over single check slot, formal slot, obligations, comparisons, and scope.
    full_matrix_count = 0
    for check_label, checks in check_slot_domain():
        for formal_label, formal in formal_slot_domain():
            for ob_mode in obligation_modes():
                obligations = build_obligations(ob_mode, checks, formal)
                for comp_label, comparisons in comparison_domain():
                    for scope_label, uncovered in scope_domain():
                        case = GateInputTuple(
                            label=f"{check_label}|{formal_label}|{ob_mode}|{comp_label}|{scope_label}",
                            checks=checks,
                            formal=formal,
                            obligations=obligations,
                            uncovered=uncovered,
                            comparisons=comparisons,
                        )
                        _record_tuple(case, digest, counts, mismatch_total, mismatches, "full_matrix")
                        full_matrix_count += 1

    # 2. Exhaustive pairwise two-check precedence space (all 8 active check states x 8 x 2 scope modes).
    pairwise_states: tuple[tuple[str, Freshness, tuple[str, ...]], ...] = tuple(
        (fresh.value, fresh, ()) for fresh in ALL_FRESHNESS
    ) + (("running", Freshness.MISSING, ("run-active",)),)
    pairwise_count = 0
    for (left_label, left_fresh, left_run), (right_label, right_fresh, right_run) in product(
        pairwise_states, repeat=2
    ):
        checks = (
            CheckSpecInput("c1", left_fresh, left_run, left_label),
            CheckSpecInput("c2", right_fresh, right_run, right_label),
        )
        obligations = {"checks": {"c1": "d", "c2": "d"}, "formal_required": {}, "dropped": {}}
        for scope_label, uncovered in (("full_tree", ()), ("scoped_one_uncovered", ("README.md",))):
            case = GateInputTuple(
                label=f"pair:{left_label}+{right_label}|{scope_label}",
                checks=checks,
                formal=(),
                obligations=obligations,
                uncovered=uncovered,
                comparisons=None,
            )
            _record_tuple(case, digest, counts, mismatch_total, mismatches, "pairwise_checks")
            pairwise_count += 1

    controls = negative_controls()
    checked = full_matrix_count + pairwise_count
    state_path = ROOT / "src" / "vkit" / "core" / "state.py"
    return {
        "schema_version": 1,
        "scope": (
            "exhaustive gate verdict and DOUBTS precedence tuples over check freshness/running states, "
            "required and optional formal states, obligation inventory/removals/dropped, "
            "weakened/incomparable comparisons, and full-tree vs scoped uncovered paths"
        ),
        "checker_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "evaluator": "src/vkit/core/state.py::gate",
        "evaluator_sha256": sha256(state_path.read_bytes()).hexdigest(),
        "dimensions": {
            "check_slot_states": [label for label, _ in check_slot_domain()],
            "formal_slot_states": [label for label, _ in formal_slot_domain()],
            "obligation_modes": list(obligation_modes()),
            "comparison_modes": [label for label, _ in comparison_domain()],
            "scope_modes": [label for label, _ in scope_domain()],
            "pairwise_check_states": [label for label, _, _ in pairwise_states],
        },
        "full_matrix_tuples_checked": full_matrix_count,
        "pairwise_check_tuples_checked": pairwise_count,
        "decision_tuples_checked": checked,
        "observed_decision_count": checked,
        "expected_verdict_counts": {
            key.replace(":expected:", ":", 1): value
            for key, value in sorted(counts.items())
            if ":expected:" in key
        },
        "observed_verdict_counts": {
            key.replace(":observed:", ":", 1): value
            for key, value in sorted(counts.items())
            if ":observed:" in key
        },
        "reason_token_counts": {
            key.replace("reason:", "", 1): value
            for key, value in sorted(counts.items())
            if key.startswith("reason:")
        },
        "mismatch_count": mismatch_total[0],
        "mismatches": mismatches,
        "comparator_negative_controls": controls,
        "tuple_space_sha256": digest.hexdigest(),
        "passed": mismatch_total[0] == 0 and controls["decision_rejected"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exhaust vkit.core.state.gate verdict and DOUBTS precedence against an independent oracle."
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = run()
    summary = {
        key: report[key]
        for key in (
            "passed",
            "full_matrix_tuples_checked",
            "pairwise_check_tuples_checked",
            "decision_tuples_checked",
            "mismatch_count",
            "comparator_negative_controls",
            "tuple_space_sha256",
        )
    }
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary["report"] = str(output)
    print(json.dumps(summary, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
