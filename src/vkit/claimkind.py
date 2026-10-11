"""The kind of claim each check category supports: what a green result establishes and what it does
not, carried with every result.
"""
from __future__ import annotations

import enum


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
