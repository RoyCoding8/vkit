from scripts.check_finite_decisions import run


def test_finite_decision_checker_exhausts_declared_space_and_rejects_mutants():
    report = run()

    assert report["passed"] is True
    assert report["policy_candidates"] == 320
    assert report["valid_policies_checked"] == 274
    assert report["conflicting_guard_policies_rejected"] == 46
    assert report["local_evidence_tuples_checked"] == 332_088
    assert report["full_domain_output_tuples_checked"] == 22_194
    assert report["aggregate_precedence_tuples_checked"] == 64
    assert report["decision_tuples_checked"] == 354_346
    assert report["observed_decision_count"] == 354_346
    assert report["mismatch_count"] == 0
    assert report["mismatches"] == []
    assert report["comparator_negative_controls"]["decision_rejected"] is True
    assert [
        control["decision_rejected"]
        for control in report["comparator_negative_controls"]["controls"]
    ] == [True, True]
    assert report["source_proof"] is False
    assert report["approval_authority"] is False
    assert len(report["tuple_space_sha256"]) == 64
    assert len(report["checker_sha256"]) == 64
    assert len(report["evaluator_sha256"]) == 64
    assert len(report["reference_model_sha256"]) == 64
    assert report["evaluator"] == "src/vkit/certification.py"
    assert report["reference_model"] == "verification/certification/Differences.tla"
    assert report["expected_status_counts"] == report["observed_status_counts"]
    assert {key: value for key, value in report["observed_status_counts"].items()
            if key.startswith("aggregate:")} == {
        "aggregate:observed_conformance": 4,
        "aggregate:observed_violation": 15,
        "aggregate:unknown": 45,
    }
