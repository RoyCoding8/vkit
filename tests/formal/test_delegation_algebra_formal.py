from __future__ import annotations

from formal.checks.check_delegation_algebra import (
    NEGATIVE_CONTROL_MUTANTS,
    RejectionReason,
    run,
)


def test_delegation_algebra_exhaustive_formal_check():
    report = run()

    assert report["passed"] is True
    assert report["aux_relations"]["passed"] is True
    assert report["aux_relations"]["compare_contracts_checked"] == 257
    assert report["aux_relations"]["intent_entails_claim_checked"] == 257

    real = report["real_code"]
    assert real["passed"] is True
    assert real["mismatches"] == []
    assert report["domain"]["exhaustive_2x2_tuples"] == 26244
    assert report["domain"]["exhaustive_1x2_tuples"] == 162
    assert real["accepted"] > 0

    # Every rejection category in the specification is exercised and verified.
    assert set(real["rejection_counts"].keys()) == {reason.name for reason in RejectionReason}

    controls = report["negative_controls"]
    assert controls["all_caught"] is True
    assert len(controls["mutants"]) == len(NEGATIVE_CONTROL_MUTANTS)
    for spec in NEGATIVE_CONTROL_MUTANTS:
        mutant_report = controls["mutants"][spec.name]
        assert mutant_report["caught"] is True
        assert mutant_report["counterexample"] is not None
        assert mutant_report["counterexample"]["kind"] == spec.expected_mismatch_kind
