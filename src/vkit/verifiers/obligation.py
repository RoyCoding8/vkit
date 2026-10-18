"""What a check must establish, as a sum of case, theorem and property obligations so a name of one
kind cannot be typed into another's field.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..outcome import ScenarioResult


@dataclass(frozen=True, order=True)
class CaseObligation:
    """One named case: a scenario id a driver reports, or a test node id."""

    test_id: str


@dataclass(frozen=True, order=True)
class TheoremObligation:
    """One named theorem in a named module. The module is explicit, never guessed from a path."""

    theorem: str
    module: str


@dataclass(frozen=True, order=True)
class PropertyObligation:
    """One named TLA+ property at one set of bounds, sorted so declaration order does not
    distinguish obligations.
    """

    property_name: str
    bounds: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", tuple(sorted(self.bounds)))


Obligation = CaseObligation | TheoremObligation | PropertyObligation


def obligation_from_json(document: dict) -> Obligation:
    """Parse one obligation, raising ValueError on an unknown kind."""
    kind = document.get("kind")
    if kind == "case":
        return CaseObligation(document["obligation"])
    if kind == "theorem":
        return TheoremObligation(document["obligation"], document["module"])
    if kind == "property":
        return PropertyObligation(
            document["obligation"],
            tuple((name, int(value)) for name, value in document["bounds"]),
        )
    known = "case, property, theorem"
    raise ValueError(
        f"unknown obligation kind {kind!r}; a required obligation is one of {known}"
    )


def obligation_to_json(obligation: Obligation) -> dict:
    """The JSON form recorded in a run."""
    if isinstance(obligation, TheoremObligation):
        return {
            "kind": "theorem",
            "obligation": obligation.theorem,
            "module": obligation.module,
        }
    if isinstance(obligation, PropertyObligation):
        return {
            "kind": "property",
            "obligation": obligation.property_name,
            "bounds": [[name, value] for name, value in obligation.bounds],
        }
    return {"kind": "case", "obligation": obligation.test_id}


def describe(obligation: Obligation) -> str:
    """A sentence naming an obligation, for findings."""
    if isinstance(obligation, TheoremObligation):
        return f"theorem {obligation.theorem!r} in module {obligation.module!r}"
    if isinstance(obligation, PropertyObligation):
        bounds = ", ".join(f"{name}={value}" for name, value in obligation.bounds)
        return f"model property {obligation.property_name!r} at {bounds or '<no bounds>'}"
    return f"case {obligation.test_id!r}"


def obligations_from_scenarios(scenarios: tuple[str, ...]) -> tuple[Obligation, ...]:
    """A list of scenario ids as case obligations."""
    return tuple(CaseObligation(scenario) for scenario in scenarios)


def scenario_results(
    observations: tuple[tuple[CaseObligation, str], ...],
    counterexamples: tuple[tuple[CaseObligation, str], ...],
) -> tuple[ScenarioResult, ...]:
    """Passing observations followed by failing counterexamples, as scenario results."""
    return tuple(ScenarioResult(o.test_id, True, text) for o, text in observations) + tuple(
        ScenarioResult(o.test_id, False, text) for o, text in counterexamples
    )


def case_results(
    observations: tuple[tuple[CaseObligation, str], ...],
    counterexamples: tuple[tuple[CaseObligation, str], ...],
) -> tuple[list[dict], list[dict]]:
    """Satisfied cases and counterexamples as run-record dicts."""
    satisfied = [
        {"kind": "case_satisfied", "obligation": obligation_to_json(o), "observation": text}
        for o, text in observations
    ]
    failed = [
        {"obligation": obligation_to_json(o), "trace": text} for o, text in counterexamples
    ]
    return satisfied, failed
