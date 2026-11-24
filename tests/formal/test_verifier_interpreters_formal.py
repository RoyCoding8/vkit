"""Pytest entrypoint for the Lean and TLC report interpreter formal checker."""
from __future__ import annotations

from formal.checks.check_verifier_interpreters import run


def test_verifier_interpreters_exhaustive_formal_check():
    summary = run()
    assert summary["ok"] is True, summary

    lean = summary["lean"]
    assert lean["reviewed_tuples"] == 864
    assert lean["comparator_tuples"] == 4
    assert lean["total_tuples"] == 868
    assert lean["pass_tuples"] == 2
    assert lean["logical_trust_checks"] == 3
    assert lean["oracle_mismatches"] == 0
    assert lean["safety_violations"] == 0

    tlc = summary["tlc"]
    assert tlc["total_tuples"] == 384
    assert tlc["pass_tuples"] == 1
    assert tlc["oracle_mismatches"] == 0
    assert tlc["safety_violations"] == 0

    mutants = summary["negative_controls"]
    assert len(mutants) == 4
    assert summary["all_mutants_caught"] is True
    for mutant in mutants:
        assert mutant["caught"] is True, mutant
        assert mutant["oracle_mismatches"] > 0, mutant
        assert mutant["safety_violations"] > 0, mutant
