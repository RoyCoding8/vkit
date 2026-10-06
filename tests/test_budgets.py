from __future__ import annotations

from vkit.budgets import Budget, Measurement, evaluate


def rows(results):
    return [(r.scenario_id, r.passed, r.observation) for r in results]


def test_limits_respect_the_direction_of_better():
    measured = (Measurement("latency", 50.0, "ms", "lower"), Measurement("throughput", 900.0, "rps", "higher"))
    results, unchecked = evaluate((Budget("latency", limit=40), Budget("throughput", limit=1000)), measured, None)
    assert rows(results) == [
        ("budget:latency:limit", False, "50 ms, at most 40 allowed"),
        ("budget:throughput:limit", False, "900 rps, at least 1000 allowed"),
    ]
    assert unchecked == ()


def test_regression_is_judged_against_the_pinned_baseline_only():
    measured = (Measurement("latency", 12.0, "ms", "lower"),)
    budget = (Budget("latency", max_regression_pct=10),)
    assert rows(evaluate(budget, measured, {"latency": 10.0})[0]) == [
        ("budget:latency:regression", False, "12 ms vs baseline 10 (+20.0%), at most 10% worse allowed")]
    assert rows(evaluate(budget, measured, {"latency": 11.5})[0])[0][1] is True
    assert evaluate(budget, measured, None) == ((), ("latency: no baseline is pinned, so regression was not checked",))


def test_a_budget_on_a_measurement_the_check_did_not_report_fails():
    results, _ = evaluate((Budget("latency", limit=1),), (), None)
    assert rows(results) == [("budget:latency", False, "the check did not report this measurement")]
