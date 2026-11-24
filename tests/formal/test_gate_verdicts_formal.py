from __future__ import annotations

from formal.checks.check_gate_verdicts import run


def test_gate_verdict_formal_checker_exhausts_domain_and_rejects_mutants():
    report = run()

    assert report["passed"] is True
    assert report["mismatch_count"] == 0
    assert report["mismatches"] == []
    assert report["full_matrix_tuples_checked"] == 10_692
    assert report["pairwise_check_tuples_checked"] == 128
    assert report["decision_tuples_checked"] == 10_820
    assert report["observed_decision_count"] == 10_820
    assert report["expected_verdict_counts"] == report["observed_verdict_counts"]
    assert report["comparator_negative_controls"]["decision_rejected"] is True
    assert [
        control["decision_rejected"]
        for control in report["comparator_negative_controls"]["controls"]
    ] == [True, True, True, True, True]
    assert report["evaluator"] == "src/vkit/core/state.py::gate"
    assert len(report["tuple_space_sha256"]) == 64
    assert len(report["checker_sha256"]) == 64
    assert len(report["evaluator_sha256"]) == 64
