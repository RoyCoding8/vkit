"""Measurements a check reported, judged against the budgets its definition declares."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .outcome import ScenarioResult


@dataclass(frozen=True)
class Measurement:
    name: str
    value: float
    unit: str
    better: Literal["lower", "higher"]

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit, "better": self.better}


@dataclass(frozen=True)
class Budget:
    name: str
    limit: float | None = None
    max_regression_pct: float | None = None


def _worse(m: Measurement, value: float, bound: float) -> bool:
    return value > bound if m.better == "lower" else value < bound


def evaluate(budgets: tuple[Budget, ...], measured: tuple[Measurement, ...],
             baseline: dict[str, float] | None) -> tuple[tuple[ScenarioResult, ...], tuple[str, ...]]:
    """One result per enforceable budget clause, plus notes for clauses nothing could check.

    A missing measurement fails its clause. A regression clause with no pinned baseline is unchecked.
    """
    by_name = {m.name: m for m in measured}
    results = []
    unchecked = []
    for budget in budgets:
        m = by_name.get(budget.name)
        if m is None:
            results.append(ScenarioResult(f"budget:{budget.name}", False, "the check did not report this measurement"))
            continue
        if budget.limit is not None:
            word = "at most" if m.better == "lower" else "at least"
            ok = not _worse(m, m.value, budget.limit)
            results.append(ScenarioResult(f"budget:{budget.name}:limit", ok,
                                          f"{m.value:g} {m.unit}, {word} {budget.limit:g} allowed"))
        if budget.max_regression_pct is not None:
            base = (baseline or {}).get(budget.name)
            if base is None:
                unchecked.append(f"{budget.name}: no baseline is pinned, so regression was not checked")
                continue
            change = 0.0 if base == 0 else (m.value - base) / abs(base) * 100
            regression = change if m.better == "lower" else -change
            ok = regression <= budget.max_regression_pct
            results.append(ScenarioResult(
                f"budget:{budget.name}:regression", ok,
                f"{m.value:g} {m.unit} vs baseline {base:g} ({change:+.1f}%), "
                f"at most {budget.max_regression_pct:g}% worse allowed"))
    return tuple(results), tuple(unchecked)
