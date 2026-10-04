"""What a check is required to establish, as three kinds of name.

docs/verification.md forbids forcing a Lean theorem name into a scenario field, and this
is the alternative: a sum type rather than a `string`. A scenario id, a theorem
declaration and a TLA+ property are three different things a receipt has to
match, and one string field would let a scenario check name a theorem by typing
it there.

Each variant is frozen, so it is hashable and two obligations compare by value.
That is what makes the policy's set difference a total order: `compare` needs
`required - declared` to mean something, and a mutable or unhashable obligation
would make the subtraction itself the thing that fails.

`bounds` on `PropertyObligation` is sorted at construction. Two declarations of
the same property at the same bounds written in a different order are one
obligation, and an unsorted tuple would report them as two.

There is deliberately no `Obligation` base class with an `id` attribute. That
would put a string back where the union is, and `TheoremObligation("x").id`
would either raise or lie.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class CaseObligation:
    """One named case: a scenario id a driver reports, or a test node id."""

    test_id: str


@dataclass(frozen=True, order=True)
class TheoremObligation:
    """One named theorem in a named module.

    `module` is required rather than derived from a path. An adapter that
    guessed the module from a filename could discharge an obligation against
    the wrong declaration, and the guess would be invisible in the receipt.
    """

    theorem: str
    module: str


@dataclass(frozen=True, order=True)
class PropertyObligation:
    """One named TLA+ property at one set of bounds.

    The bounds belong to the obligation rather than to the receipt, because a
    run at different bounds is a different claim. Recording them here means two
    runs at different bounds produce two obligations rather than one obligation
    with two observations.
    """

    property_name: str
    bounds: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", tuple(sorted(self.bounds)))


Obligation = CaseObligation | TheoremObligation | PropertyObligation


def obligation_from_json(document: dict) -> Obligation:
    """Read one obligation as vkit's own value, refusing an unknown shape.

    Parse, not cast. A document that names a kind the union does not have is a
    refusal rather than a `CaseObligation` with a mangled field, because a
    theorem silently read as a case is exactly the confusion this module exists
    to prevent.
    """
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
    """The wire form, matching `receipt.v2.json`'s obligation definitions."""
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
    """The sentence a finding quotes when an obligation went missing."""
    if isinstance(obligation, TheoremObligation):
        return f"theorem {obligation.theorem!r} in module {obligation.module!r}"
    if isinstance(obligation, PropertyObligation):
        bounds = ", ".join(f"{name}={value}" for name, value in obligation.bounds)
        return f"model property {obligation.property_name!r} at {bounds or '<no bounds>'}"
    return f"case {obligation.test_id!r}"


def obligations_from_scenarios(scenarios: tuple[str, ...]) -> tuple[Obligation, ...]:
    """The v1 reading of a scenario list, as the case obligations it always was.

    A v1 `required_scenarios` entry is a case. This is the one place the old
    spelling is understood, and it lives here rather than in the parser so that
    every caller converting a v1 list goes through the same conversion.
    """
    return tuple(CaseObligation(scenario) for scenario in scenarios)
