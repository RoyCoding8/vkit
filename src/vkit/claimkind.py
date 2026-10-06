"""What kind of check produced this result, and what that kind can support.

A check that says PASS says something, but not everything. Four kinds of check
exist in this repository and they do not carry the same weight. Conflating them
is how a product ends up described as "formally verified" when it has in fact
been scenario-tested, and that sentence is the failure this module exists to
prevent.

The categories, and what a green result in each one licenses you to say.

  SCENARIO           A named example was run and its observed output compared
                     against a literal expected value. Establishes that THIS
                     sequence of calls produced THIS answer. Says nothing about
                     any other sequence. Most of tests/ is in this category,
                     and it is the only category that needs no toolchain.

  PROPERTY           Hypothesis generated operation sequences and no
                     disagreement was found between the code under test and a
                     separate reference model. Establishes correspondence over
                     the sequences tried. Does NOT establish implementation
                     equivalence: the two differ mechanically, and Hypothesis
                     samples a bounded family rather than exhausting it.

  FINITE MODEL       TLC explored every reachable state of a finite TLA+ model
                     and the checked properties held in all of them.
                     Establishes the stated properties for THAT model at THAT
                     configuration. Says nothing about a different number of
                     workers, a different number of revisions, or any
                     implementation in any language. The configuration is
                     recorded in the receipt and a run whose configuration
                     differs is refused rather than reported.

  THEOREM CHECKING   A Lean theorem relating an executable decision to a
                     logical specification, with an axiom audit. Establishes
                     the theorem for the Lean model. Says nothing about the
                     Python core, and the shared example records are a
                     countercheck of specific decisions rather than a proof
                     that the two agree.

## Why the category has to survive into the summary

A generic "PASS" is true and useless. The same green tick covers a test that
proves the product works on one input and a model check that proves a property
over 207,360 states, and a reader who sees only the tick cannot tell which they
have. So a result carries its category with it, `ClaimCategory` below, and
`describe` renders the category and its scope together.

The strongest thing any of these can support, taken together, is "the
ownership and acceptance rules behaved correctly on every case we ran and on
every state of one finite model of them". That is a real sentence. "The app is
formally verified" is a different and unsupported one.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class ClaimCategory(enum.Enum):
    """The kind of check, and therefore the kind of claim it supports."""

    SCENARIO = "scenario"
    PROPERTY = "property"
    STATIC = "static_analysis"
    FINITE_MODEL = "finite_model_checking"
    THEOREM = "theorem_checking"

    @property
    def establishes(self) -> str:
        return {
            ClaimCategory.SCENARIO: (
                "this named sequence of calls produced this observed result"
            ),
            ClaimCategory.PROPERTY: (
                "no disagreement was found between the code and a separate "
                "reference model over the generated sequences"
            ),
            ClaimCategory.STATIC: (
                "the analyzer reported no finding outside the baseline a human accepted"
            ),
            ClaimCategory.FINITE_MODEL: (
                "the checked properties hold in every reachable state of one "
                "finite model at one recorded configuration"
            ),
            ClaimCategory.THEOREM: (
                "the stated theorem holds for the Lean model, with the axioms "
                "audited"
            ),
        }[self]

    @property
    def does_not_establish(self) -> str:
        return {
            ClaimCategory.SCENARIO: (
                "anything about a sequence that was not run"
            ),
            ClaimCategory.PROPERTY: (
                "implementation equivalence; the models differ mechanically and "
                "the sequences are sampled, not exhausted"
            ),
            ClaimCategory.STATIC: (
                "the absence of defects the analyzer's rules cannot see"
            ),
            ClaimCategory.FINITE_MODEL: (
                "any other number of workers, revisions or resources, and any "
                "implementation in any language"
            ),
            ClaimCategory.THEOREM: (
                "anything about the Python core"
            ),
        }[self]

    @property
    def needs_toolchain(self) -> str | None:
        """The opt-in tool this category needs, or None for none.

        Formal tooling is opt-in. Nothing in this module downloads anything,
        and no hook or ordinary MCP check call reaches it, which is what
        docs/verification.md requires. An absent toolchain is a BLOCKED result for this
        category and no effect at all on the others.
        """
        return {
            ClaimCategory.SCENARIO: None,
            ClaimCategory.PROPERTY: "hypothesis (a Python package)",
            ClaimCategory.STATIC: "the analyzer the check runs",
            ClaimCategory.FINITE_MODEL: "a JRE and tla2tools.jar",
            ClaimCategory.THEOREM: "the Lean toolchain",
        }[self]


@dataclass(frozen=True)
class Result:
    """One check result, with the category that decides how to read it."""

    category: ClaimCategory
    status: str
    scope: str

    def __post_init__(self) -> None:
        if self.status not in ("PASS", "FAIL", "BLOCKED"):
            raise ValueError(
                f"status {self.status!r} is not one of PASS, FAIL, BLOCKED. A "
                f"result is one of those three; anything else is a lie about "
                f"which one it is."
            )

    def describe(self) -> str:
        category = self.category
        return (
            f"[{category.value}] {self.status}: {category.establishes}. "
            f"It does not establish {category.does_not_establish}. "
            f"Scope of this run: {self.scope}"
        )

    def to_json(self) -> dict:
        return {
            "category": self.category.value,
            "status": self.status,
            "establishes": self.category.establishes,
            "does_not_establish": self.category.does_not_establish,
            "scope": self.scope,
        }
