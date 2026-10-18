from __future__ import annotations

import pytest

from vkit.operations import matchsets
from vkit.operations.matchsets import compare_matchsets


def test_returns_shortest_witness_in_each_direction() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("a|aa", "aa|aaa", "a")

    assert result.status == "COUNTEREXAMPLE"
    assert result.old_only == "a"
    assert result.new_only == "aaa"
    assert result.backend == "greenery"
    assert result.backend_version == "4.2.2"
    assert result.dialect == "greenery-4.2.2"


def test_difference_is_restricted_to_the_supplied_alphabet() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("[^a]", "d", "cd")

    assert result.status == "COUNTEREXAMPLE"
    assert result.old_only == "c"
    assert result.new_only is None


def test_wildcard_includes_newline_in_the_named_dialect() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets(".", "a|\n", "a\n")

    assert result.status == "EQUIVALENT"
    assert result.old_only is None
    assert result.new_only is None


def test_empty_string_witness_is_distinct_from_no_witness() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("", "a+", "a")

    assert result.status == "COUNTEREXAMPLE"
    assert result.old_only == ""
    assert result.new_only == "a"


def test_empty_alphabet_contains_only_the_empty_string() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("a*", "", "")

    assert result.status == "EQUIVALENT"
    assert result.old_only is None
    assert result.new_only is None


def test_unicode_alphabet_is_compared_as_codepoints() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("🙂", ".", "🙂")

    assert result.status == "EQUIVALENT"
    assert result.alphabet == "🙂"


def test_nonregular_construct_is_reported_as_unsupported() -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets("(?=a)a", "a", "a")

    assert result.status == "UNSUPPORTED"
    assert result.old_only is None
    assert result.new_only is None


@pytest.mark.parametrize("pattern", ("[", "[z-a]", r"\x"))
def test_malformed_pattern_syntax_is_reported_as_unsupported(pattern: str) -> None:
    pytest.importorskip("greenery")

    result = compare_matchsets(pattern, "a", "a")

    assert result.status == "UNSUPPORTED"
    assert result.old_only is None
    assert result.new_only is None
    assert result.reason == "pattern is invalid in the greenery-4.2.2 syntax"


def test_pattern_and_timeout_bounds_are_checked() -> None:
    with pytest.raises(ValueError, match="2048 UTF-8 bytes"):
        compare_matchsets("a" * 2_049, "a", "a")
    with pytest.raises(ValueError, match="unique codepoints"):
        compare_matchsets("a", "a", "aa")
    with pytest.raises(ValueError, match="timeout_ms"):
        compare_matchsets("a", "a", "a", timeout_ms=True)


def test_worker_timeout_is_reported_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise matchsets.subprocess.TimeoutExpired("worker", 0.001)

    monkeypatch.setattr(matchsets.subprocess, "run", timed_out)

    result = compare_matchsets("a", "b", "ab", timeout_ms=1)

    assert result.status == "UNKNOWN"
    assert result.old_only is None
    assert result.new_only is None
