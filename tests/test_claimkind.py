"""The claim category has to survive into the summary, and it has a test.

`src/vkit/claimkind.py` exists because a bare PASS is true and useless. The
same word covers a test that exercises one input and a model check that
exhausts 207,360 states, and a reader holding only the word cannot tell which
they have. These tests pin the two things that make the category useful.

The first is that a result cannot report a status outside PASS, FAIL and
BLOCKED. An in-progress, skipped or unknown status is a lie about which of the
three it is, and accepting one would let "BLOCKED" become "not applicable",
which is the same as a pass.

The second is that a described result always carries its own limits. A
description that says what a run establishes without saying what it does not is
worse than no description, because it reads as a stronger claim than the
evidence supports. Each assertion below removes a clause and expects the
category's own wording to still be there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.claimkind import ClaimCategory, Result  # noqa: E402


def test_a_result_must_name_one_of_the_three_statuses() -> None:
    for bad in ("OK", "pass", "SKIPPED", "in progress", ""):
        with pytest.raises(ValueError):
            Result(ClaimCategory.SCENARIO, bad, "a scope")


def test_a_blocked_result_is_a_status_and_not_a_pass() -> None:
    """An absent toolchain is BLOCKED for its category and silent for the rest."""
    result = Result(ClaimCategory.FINITE_MODEL, "BLOCKED", "no tla2tools.jar on this host")
    assert result.status == "BLOCKED"
    assert result.status != "PASS"


def test_every_category_says_what_it_does_not_establish() -> None:
    for category in ClaimCategory:
        result = Result(category, "PASS", "the recorded run")
        described = result.describe()
        assert category.establishes in described
        assert category.does_not_establish in described
        assert "It does not establish" in described


def test_a_finite_model_pass_does_not_survive_as_a_general_claim() -> None:
    """The sentence Plan 08 exists to prevent, checked on the real renderer.

    A model check over two workers says something about two workers. The
    description has to name the bound every time it is produced, so a reader
    cannot carry the word PASS forward and drop the rest of the sentence.
    """
    result = Result(
        ClaimCategory.FINITE_MODEL, "PASS",
        "2 owners, 2 resources, 2 checks, 2 revisions, 2 generations; "
        "1,935,362 states generated, 207,360 distinct, depth 19, queue empty",
    )
    described = result.describe()
    assert "other number of workers" in described
    assert "implementation in any language" in described
    assert "2 owners" in described


def test_scenario_checks_need_no_toolchain_and_the_others_say_theirs() -> None:
    """Formal tooling is opt-in, and the category is where that is recorded."""
    assert ClaimCategory.SCENARIO.needs_toolchain is None
    assert ClaimCategory.PROPERTY.needs_toolchain == "hypothesis (a Python package)"
    assert ClaimCategory.FINITE_MODEL.needs_toolchain == "a JRE and tla2tools.jar"
    assert ClaimCategory.THEOREM.needs_toolchain == "the Lean toolchain"


def test_the_json_form_carries_the_category_and_both_limits() -> None:
    payload = Result(ClaimCategory.PROPERTY, "PASS", "200 sequences").to_json()
    assert payload["category"] == "property"
    assert payload["establishes"]
    assert payload["does_not_establish"]
    assert "implementation equivalence" in payload["does_not_establish"]
