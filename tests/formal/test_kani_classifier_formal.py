from __future__ import annotations

from formal.checks import check_kani_classifier


def test_kani_classifier_exhaustive_formal_check() -> None:
    report = check_kani_classifier.run()

    assert report["ok"] is True, report
    assert report["violations"] == []
    assert report["tuples_checked"] == (5**6) * 3
    assert report["counter_cases_checked"] >= 30

    kind_counts = report["kind_counts"]
    assert kind_counts["verified"] == 1
    assert kind_counts["proof_rejected"] > 0
    assert kind_counts["unknown"] > 0

    negative_controls = report["negative_controls"]
    assert len(negative_controls) >= 3
    for control in negative_controls:
        assert control["caught"] is True, control
        assert control["first_violation"] is not None, control
